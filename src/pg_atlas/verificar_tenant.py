"""Medição do efeito do filtro de tenant correlacionado (tarefa 2.5, decisão D-019 item 6).

Opção de teste do comando `relacionamentos verificar --medir-tenant N`: para até N pares que foram
amostrados com filtro `correlacionado`, repete a amostragem SEM a condição de correlação e grava só
contagens e taxas (com e sem filtro) em `<doc>/relacionamentos/tenant-efeito.json`.

Não altera `verificacoes.json` nem `grafo.json`. Nenhum valor de dado sai do banco: a consulta é a mesma
do cache e devolve apenas `amostrados` e `casados`. Conecta só pela `Conexao`, com o freio de carga da 2.4.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

import jsonschema

from pg_atlas.erros import ErroExecucao, SqlRejeitado
from pg_atlas.json_util import escrever_json
from pg_atlas.relacionamentos_codigo import FONTE_CODIGO
from pg_atlas.verificar_amostragem import (
    FILTRO_CORRELACIONADO,
    FILTRO_NENHUM,
    Chave,
    Limiares,
    Plano,
    montar_consulta,
)
from pg_atlas.verificar_aplicar import Registro, chave_da_aresta

SAIDA_TENANT = ("relacionamentos", "tenant-efeito.json")
LIMIAR_DIFERENCA = 0.05  # diferença de taxa (sem filtro menos com filtro) considerada relevante
FALHAS_SEGUIDAS_PARA_FREIO = 3
CODIGOS_DE_FREIO = {"57014", "55P03"}


def pares_chave_sem_tenant(arestas: list[dict[str, Any]]) -> set[Chave]:
    """Pares cuja evidência de código marca `chave_sem_tenant`."""
    return {
        chave_da_aresta(a)
        for a in arestas
        if any(e.get("tipo") == FONTE_CODIGO and e.get("chave_sem_tenant") for e in a["evidencia"])
    }


def escolher_pares(cache: dict[Chave, Registro], prioritarios: set[Chave], n: int) -> list[Chave]:
    """Até `n` pares correlacionados e verificados: os prioritários primeiro, depois os demais espaçados."""
    elegiveis = [
        k
        for k in sorted(cache)
        if cache[k]["filtro_tenant"] == FILTRO_CORRELACIONADO and cache[k]["situacao"] == "verificado"
    ]
    primeiros = [k for k in elegiveis if k in prioritarios][:n]
    restantes = [k for k in elegiveis if k not in prioritarios]
    falta = min(n - len(primeiros), len(restantes))
    if falta <= 0:
        return primeiros
    passo = len(restantes) / falta
    return primeiros + [restantes[int(i * passo)] for i in range(falta)]


def _taxa(casados: int, amostrados: int) -> float | None:
    return round(casados / amostrados, 4) if amostrados > 0 else None


def medir_par(
    conexao: Any, chave: Chave, reg: Registro, tipos_iguais: bool, tenant: str | None, lim: Limiares
) -> dict[str, Any]:
    """Reamostra o par sem correlação de tenant e devolve a linha do relatório (só contagens)."""
    plano = Plano(reg["metodo"], reg["pct"], FILTRO_NENHUM)
    consulta = montar_consulta(chave, plano, tipos_iguais, tenant, lim.amostra_max)
    (so, ro), co, (sd, rd), cd = chave
    linha: dict[str, Any] = {
        "origem": {"schema": so, "relacao": ro, "colunas": list(co)},
        "destino": {"schema": sd, "relacao": rd, "colunas": list(cd)},
        "com_filtro": {"amostrados": reg["amostrados"], "casados": reg["casados"], "taxa": reg["taxa"]},
        "sem_filtro": None,
        "erro": None,
        "diferenca": None,
    }
    try:
        linhas = conexao.executar(consulta)
    except (ErroExecucao, SqlRejeitado) as erro:
        linha["erro"] = str(getattr(erro, "sqlstate", None) or type(erro).__name__)
        return linha
    amostrados, casados = int(linhas[0][0]), int(linhas[0][1])
    taxa = _taxa(casados, amostrados)
    linha["sem_filtro"] = {"amostrados": amostrados, "casados": casados, "taxa": taxa}
    if taxa is not None and reg["taxa"] is not None:
        linha["diferenca"] = round(taxa - reg["taxa"], 4)
    return linha


def resumir(linhas: list[dict[str, Any]], sem_tenant: int, parou_por_freio: bool) -> dict[str, Any]:
    diferencas = [x["diferenca"] for x in linhas if x["diferenca"] is not None]
    return {
        "pares_medidos": len(diferencas),
        "pares_chave_sem_tenant": sem_tenant,
        "pares_com_erro": sum(1 for x in linhas if x["erro"]),
        "diferenca_maxima": max(diferencas) if diferencas else None,
        "diferenca_media": round(sum(diferencas) / len(diferencas), 4) if diferencas else None,
        "pares_com_diferenca_relevante": sum(1 for d in diferencas if abs(d) > LIMIAR_DIFERENCA),
        "pares_com_taxa_sem_filtro_maior": sum(1 for d in diferencas if d > 0),
        "limiar_diferenca": LIMIAR_DIFERENCA,
        "interrompido_por_freio": parou_por_freio,
    }


def validar_relatorio(documento: Any) -> None:
    texto = (resources.files("pg_atlas.esquemas") / "tenant-efeito.schema.json").read_text(encoding="utf-8")
    jsonschema.Draft202012Validator(json.loads(texto)).validate(documento)


def _ordem(linha: dict[str, Any]) -> tuple[Any, ...]:
    o, d = linha["origem"], linha["destino"]
    return (o["schema"], o["relacao"], o["colunas"], d["schema"], d["relacao"], d["colunas"])


def medir_efeito_tenant(
    conexao: Any,
    doc: Path,
    cache: dict[Chave, Registro],
    pares: dict[Chave, bool],
    arestas: list[dict[str, Any]],
    tenant: str | None,
    lim: Limiares,
    n: int,
    *,
    pausa_ms: int = 0,
    dormir: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Mede até `n` pares e grava `tenant-efeito.json`. Para ao ver 3 timeouts ou falhas de lock seguidos."""
    prioritarios = pares_chave_sem_tenant(arestas)
    escolhidos = escolher_pares(cache, prioritarios, n)
    linhas: list[dict[str, Any]] = []
    medidos: list[Chave] = []
    seguidos, freio = 0, False
    for chave in escolhidos:
        linha = medir_par(conexao, chave, cache[chave], pares.get(chave, True), tenant, lim)
        linhas.append(linha)
        medidos.append(chave)
        seguidos = seguidos + 1 if linha["erro"] in CODIGOS_DE_FREIO else 0
        if seguidos >= FALHAS_SEGUIDAS_PARA_FREIO:
            freio = True
            break
        if pausa_ms > 0:
            dormir(pausa_ms / 1000)
    documento = {
        "formato": "tenant-efeito",
        "versao_formato": 1,
        "solicitados": n,
        "resumo": resumir(linhas, sum(1 for k in medidos if k in prioritarios), freio),
        "pares": sorted(linhas, key=_ordem),
    }
    validar_relatorio(documento)
    escrever_json(doc.joinpath(*SAIDA_TENANT), documento)
    return documento
