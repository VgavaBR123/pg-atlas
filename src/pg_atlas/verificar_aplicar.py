"""Aplicação do cache de verificações ao grafo (decisão D-019, itens 4 e 5). Offline e determinística.

Escolha de idempotência: `aplicar_cache` parte sempre do grafo atual, mas primeiro devolve cada aresta
inferida ao estado "cru" (confiança `hipotese`, sem taxa, sem evidência `amostragem`, sem as notas de
verificação e com a nota de ambiguidade original, recomposta de `candidatos`). Em seguida aplica o cache.
Aplicar duas vezes dá o mesmo grafo. Arestas descartadas saem do grafo e só voltam se `nome` ou
`codigo` forem reexecutados (o que também remove o bloco `resumo.verificacao`).
"""

from __future__ import annotations

import copy
from collections import Counter
from typing import Any

from pg_atlas.relacionamentos import FONTE_FK
from pg_atlas.relacionamentos_codigo import FONTE_CODIGO
from pg_atlas.relacionamentos_nome import FONTE_NOME, TETO_DESTINOS
from pg_atlas.verificar_amostragem import VERSAO_METODO, Chave, Limiares

FONTES_INFERIDAS = (FONTE_NOME, FONTE_CODIGO)
TIPO_AMOSTRAGEM = "amostragem"
PREFIXO_AMBIGUO = "ambíguo:"
PREFIXOS_VERIFICACAO = ("taxa de casamento", "amostra pequena", "sem verificação")
SEPARADOR = "; "
SEM_VERIFICACAO = {
    "sem_select": "sem verificação: sem permissão de leitura",
    "amostra_vazia": "sem verificação: amostra vazia",
}
COM_TAXA = ("confirmado", "hipotese", "pequena", "descartado")

Registro = dict[str, Any]


# ----------------------------------------------------------------- chaves e estado cru


def chave_da_aresta(a: dict[str, Any]) -> Chave:
    o, d = a["origem"], a["destino"]
    return ((o["schema"], o["relacao"]), tuple(o["colunas"]), (d["schema"], d["relacao"]), tuple(d["colunas"]))


def chave_do_registro(r: Registro) -> Chave:
    o, d = r["origem"], r["destino"]
    return ((o["schema"], o["relacao"]), tuple(o["colunas"]), (d["schema"], d["relacao"]), tuple(d["colunas"]))


def eh_fk_nao_validada(a: dict[str, Any]) -> bool:
    return a["fonte"] == FONTE_FK and any(e.get("validada") is False for e in a["evidencia"])


def eh_alvo(a: dict[str, Any]) -> bool:
    """Aresta que entra na verificação: hipóteses por nome ou código e FKs NOT VALID."""
    return a["fonte"] in FONTES_INFERIDAS or eh_fk_nao_validada(a)


def _segmentos(nota: str | None) -> list[str]:
    return [s for s in (nota or "").split(SEPARADOR) if s]


def _juntar(segmentos: list[str]) -> str | None:
    return SEPARADOR.join(segmentos) if segmentos else None


def texto_ambiguo(total: int) -> str:
    return f"{PREFIXO_AMBIGUO} {total} destinos possíveis"


def restaurar_base(a: dict[str, Any]) -> dict[str, Any]:
    """Cópia da aresta no estado anterior à verificação."""
    b = copy.deepcopy(a)
    b["taxa_casamento"] = None
    b["evidencia"] = [e for e in b["evidencia"] if e.get("tipo") != TIPO_AMOSTRAGEM]
    segmentos = [s for s in _segmentos(b["nota"]) if not s.startswith(PREFIXOS_VERIFICACAO)]
    if b["fonte"] in FONTES_INFERIDAS:
        b["confianca"] = "hipotese"
    if b["fonte"] == FONTE_NOME:
        total = b["evidencia"][0].get("candidatos", 1)
        segmentos = [texto_ambiguo(total) if s.startswith(PREFIXO_AMBIGUO) else s for s in segmentos]
    b["nota"] = _juntar(segmentos)
    return b


# ----------------------------------------------------------------- desfecho de um par


def classificar(reg: Registro | None, lim: Limiares) -> str:
    """`pendente`, `sem_select`, `amostra_vazia`, `erro`, `pequena`, `confirmado`, `hipotese` ou `descartado`."""
    if reg is None or reg["versao_metodo"] != VERSAO_METODO:
        return "pendente"
    if reg["situacao"] != "verificado":
        return str(reg["situacao"])
    if reg["amostrados"] < lim.amostra_min:
        return "pequena"
    if reg["taxa"] >= lim.confirmado:
        return "confirmado"
    if reg["taxa"] >= lim.hipotese:
        return "hipotese"
    return "descartado"


def _evidencia(reg: Registro) -> dict[str, Any]:
    return {
        "tipo": TIPO_AMOSTRAGEM,
        "amostrados": reg["amostrados"],
        "casados": reg["casados"],
        "metodo": reg["metodo"],
        "pct": reg["pct"],
        "filtro_tenant": reg["filtro_tenant"],
    }


def _nota_de_verificacao(a: dict[str, Any], reg: Registro | None, desfecho: str, lim: Limiares) -> list[str]:
    if desfecho == "pendente" or reg is None:
        return []
    if desfecho in SEM_VERIFICACAO:
        return [SEM_VERIFICACAO[desfecho]]
    if desfecho == "erro":
        return [f"sem verificação: erro ({reg['erro']})"]
    pequena = f"amostra pequena ({reg['amostrados']} valores)"
    if eh_fk_nao_validada(a):
        medida = f"taxa de casamento medida: {reg['taxa']:.4f}"
        return [medida, pequena] if desfecho == "pequena" else [medida]
    if desfecho == "pequena":
        return [pequena]
    if desfecho == "hipotese":
        return [f"taxa de casamento abaixo de {lim.confirmado * 100:g}%"]
    return []


def _aplicar_na_aresta(a: dict[str, Any], reg: Registro | None, desfecho: str, lim: Limiares) -> str | None:
    """Atualiza a aresta (confiança, taxa, evidência) e devolve o texto de verificação da nota."""
    if reg is not None and desfecho in COM_TAXA:
        a["taxa_casamento"] = reg["taxa"]
        if a["fonte"] in FONTES_INFERIDAS and desfecho == "confirmado":
            a["confianca"] = "confirmado"
    if reg is not None and (desfecho in COM_TAXA or desfecho == "amostra_vazia"):
        a["evidencia"].append(_evidencia(reg))
    return _juntar(_nota_de_verificacao(a, reg, desfecho, lim))


# ----------------------------------------------------------------- ambiguidade


def _marcar_desempate(a: dict[str, Any]) -> None:
    for e in a["evidencia"]:
        if e.get("tipo") == TIPO_AMOSTRAGEM:
            e["desempate"] = TIPO_AMOSTRAGEM


def _reduzir_ambiguas(arestas: list[dict[str, Any]]) -> tuple[int, int]:
    """Recalcula o N das notas "ambíguo" sobre as arestas que sobraram; devolve (desempatadas, reduzidas)."""
    grupos: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for a in arestas:
        if a["fonte"] == FONTE_NOME and a["evidencia"][0].get("candidatos", 1) > 1:
            o = a["origem"]
            grupos.setdefault((o["schema"], o["relacao"], o["colunas"][0]), []).append(a)
    desempatadas = reduzidas = 0
    for grupo in grupos.values():
        total = grupo[0]["evidencia"][0]["candidatos"]
        if len(grupo) == 1:
            a = grupo[0]
            a["nota"] = _juntar([s for s in _segmentos(a["nota"]) if not s.startswith(PREFIXO_AMBIGUO)])
            if a["confianca"] == "confirmado":
                _marcar_desempate(a)
                desempatadas += 1
            else:
                reduzidas += 1
        elif len(grupo) < total <= TETO_DESTINOS:  # acima do teto, as arestas já eram um subconjunto
            for a in grupo:
                a["nota"] = _juntar(
                    [texto_ambiguo(len(grupo)) if s.startswith(PREFIXO_AMBIGUO) else s for s in _segmentos(a["nota"])]
                )
    return desempatadas, reduzidas


# ----------------------------------------------------------------- aplicação


def aplicar_cache(
    arestas: list[dict[str, Any]], cache: dict[Chave, Registro], lim: Limiares
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Arestas com o cache aplicado (descartadas removidas) e o bloco `resumo.verificacao`."""
    desfechos: dict[Chave, str] = {}
    notas: dict[str, str | None] = {}
    mantidas: list[dict[str, Any]] = []
    for original in arestas:
        if not eh_alvo(original):
            mantidas.append(original)
            continue
        a = restaurar_base(original)
        chave = chave_da_aresta(a)
        reg = cache.get(chave)
        desfecho = desfechos.setdefault(chave, classificar(reg, lim))
        if desfecho == "descartado" and a["fonte"] in FONTES_INFERIDAS:
            continue
        notas[a["id"]] = _aplicar_na_aresta(a, reg, desfecho, lim)
        mantidas.append(a)
    desempatadas, reduzidas = _reduzir_ambiguas(mantidas)
    for a in mantidas:
        if notas.get(a["id"]):
            a["nota"] = _juntar(_segmentos(a["nota"]) + _segmentos(notas[a["id"]]))
    return mantidas, montar_bloco(mantidas, cache, lim, desempatadas, reduzidas)


def montar_bloco(
    arestas: list[dict[str, Any]], cache: dict[Chave, Registro], lim: Limiares, desempatadas: int, reduzidas: int
) -> dict[str, Any]:
    """Bloco `resumo.verificacao`, em pares únicos. Descartadas: pares do cache com esse desfecho fora do grafo."""
    restantes: dict[Chave, list[dict[str, Any]]] = {}
    for a in arestas:
        if eh_alvo(a):
            restantes.setdefault(chave_da_aresta(a), []).append(a)
    descartadas = sum(1 for k, r in cache.items() if k not in restantes and classificar(r, lim) == "descartado")
    desfecho = {k: classificar(cache.get(k), lim) for k in restantes}
    inferidos = {k for k, v in restantes.items() if any(x["fonte"] in FONTES_INFERIDAS for x in v)}
    registros = [cache[k] for k in restantes if desfecho[k] != "pendente"]
    contagem = Counter(desfecho.values())
    return {
        "pares": len(restantes) + descartadas,
        "verificados": sum(r["situacao"] == "verificado" for r in registros) + descartadas,
        "confirmadas": sum(1 for k in inferidos if desfecho[k] == "confirmado"),
        "hipoteses": sum(1 for k in inferidos if desfecho[k] == "hipotese"),
        "descartadas": descartadas,
        "sem_select": contagem["sem_select"],
        "amostra_vazia": contagem["amostra_vazia"],
        "amostra_pequena": contagem["pequena"],
        "pendentes": contagem["pendente"],
        "erros": dict(sorted(Counter(str(r["erro"]) for r in registros if r["situacao"] == "erro").items())),
        "desempatadas": desempatadas,
        "ambiguas_reduzidas": reduzidas,
        "not_valid_medidas": sum(
            1 for k in restantes if k not in inferidos and desfecho[k] in COM_TAXA
        ),
        "por_filtro_tenant": dict(sorted(Counter(r["filtro_tenant"] for r in registros).items())),
        "por_metodo": dict(sorted(Counter(r["metodo"] for r in registros if r["metodo"]).items())),
        "versao_metodo": VERSAO_METODO,
    }
