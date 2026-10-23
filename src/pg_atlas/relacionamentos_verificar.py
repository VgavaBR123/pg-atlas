"""Etapa `relacionamentos verificar`: verificação por amostragem (decisão D-019).

É a única etapa da fase 2 que conecta ao banco, sempre pela `Conexao` (somente leitura, guarda de SQL,
`statement_timeout`). Para cada par candidato único (hipóteses por nome ou código e FKs NOT VALID) conta,
numa amostra da coluna filha, quantos valores existem na tabela pai. Guarda só contagens e taxas em
`<doc>/relacionamentos/verificacoes.json` (o cache); nenhum valor de dado é lido para fora do banco.

Retomar é o padrão: pares já no cache com a `versao_metodo` atual não são reamostrados (`--refazer` ignora
o cache). Ao fim, o cache é aplicado ao grafo (`verificar_aplicar.py`); `--aplicar` faz só isso, sem banco.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any

import jsonschema

from pg_atlas.cli import adicionar_argumentos_comuns
from pg_atlas.conexao import Conexao
from pg_atlas.config import Projeto, carregar_projeto
from pg_atlas.erros import ErroExecucao, SqlRejeitado
from pg_atlas.json_util import escrever_json
from pg_atlas.relacionamentos import (
    SAIDA,
    ErroRelacionamentos,
    carregar_estruturas,
    ler_grafo_existente,
    montar_grafo,
    validar,
)
from pg_atlas.relacionamentos_codigo import FONTE_CODIGO
from pg_atlas.relacionamentos_nome import coluna_de_tenant
from pg_atlas.verificar_amostragem import (
    FILTRO_NENHUM,
    VERSAO_METODO,
    Chave,
    Limiares,
    Plano,
    calcular_pct,
    filtro_de_tenant,
    ler_estatisticas,
    limiares_do_projeto,
    linhas_estimadas,
    mapa_de_particoes,
    montar_consulta,
)
from pg_atlas.verificar_aplicar import (
    FONTES_INFERIDAS,
    Registro,
    aplicar_cache,
    chave_da_aresta,
    chave_do_registro,
    eh_fk_nao_validada,
)

SAIDA_CACHE = ("relacionamentos", "verificacoes.json")
SALVAR_A_CADA = 100
FALHAS_SEGUIDAS_PARA_FREIO = 3
CODIGOS_DE_FREIO = {"57014": "timeout", "55P03": "lock"}
CODIGO_SAIDA_FREIO = 2
MENSAGEM_FREIO = {
    "timeout": "{n} consultas seguidas estouraram o tempo limite",
    "lock": "{n} consultas seguidas falharam por espera de lock",
}


# ----------------------------------------------------------------- cache


def carregar_esquema_cache() -> dict[str, Any]:
    texto = (resources.files("pg_atlas.esquemas") / "verificacoes.schema.json").read_text(encoding="utf-8")
    return json.loads(texto)


def validar_cache(documento: Any, rotulo: str = "verificacoes.json") -> None:
    erros = sorted(
        jsonschema.Draft202012Validator(carregar_esquema_cache()).iter_errors(documento),
        key=lambda e: [str(p) for p in e.path],
    )
    if erros:
        onde = "/".join(str(p) for p in erros[0].absolute_path) or "(raiz)"
        raise ErroRelacionamentos(
            f"{rotulo} fora do formato em {onde}: {erros[0].message[:200]} ({len(erros)} problema(s) no total)."
        )


def carregar_cache(caminho: Path) -> dict[Chave, Registro]:
    """Cache de verificações por chave do par; arquivo ausente é cache vazio; inválido falha sem sobrescrever."""
    if not caminho.is_file():
        return {}
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ErroRelacionamentos(f"{caminho} existe mas não é um JSON válido; nada foi sobrescrito.") from None
    validar_cache(dados, f"{caminho.name} existente")
    return {chave_do_registro(r): r for r in dados["registros"]}


def gravar_cache(caminho: Path, cache: dict[Chave, Registro]) -> None:
    """Grava de forma atômica e determinística, com os registros ordenados pela chave do par."""
    registros = [cache[k] for k in sorted(cache)]
    documento = {"formato": "verificacoes", "versao_formato": 1, "registros": registros}
    validar_cache(documento)
    escrever_json(caminho, documento)


# ----------------------------------------------------------------- pares


def coletar_pares(arestas: list[dict[str, Any]]) -> dict[Chave, bool]:
    """Pares únicos a verificar, com `tipos_iguais` (falso se alguma evidência de código diz que os tipos diferem)."""
    pares: dict[Chave, bool] = {}
    for a in arestas:
        if a["fonte"] not in FONTES_INFERIDAS and not eh_fk_nao_validada(a):
            continue
        iguais = all(e.get("tipos_iguais", True) for e in a["evidencia"] if e.get("tipo") == FONTE_CODIGO)
        chave = chave_da_aresta(a)
        pares[chave] = pares.get(chave, True) and iguais
    return pares


# ----------------------------------------------------------------- contexto do catálogo


@dataclass
class Contexto:
    """O que o catálogo diz sobre as relações dos pares (acesso, colunas, estatísticas) e o tenant."""

    relacoes: dict[tuple[str, str], dict[str, Any]]
    estatisticas: dict[tuple[str, str], dict[str, Any]]
    filhos: dict[tuple[str, str], list[tuple[str, str]]]
    tenant: str | None
    colunas: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)

    def tipos_de(self, relacao: tuple[str, str]) -> dict[str, str]:
        if relacao not in self.colunas:
            rel = self.relacoes.get(relacao, {})
            self.colunas[relacao] = {c["nome"]: str(c.get("tipo")) for c in rel.get("colunas", [])}
        return self.colunas[relacao]


def montar_contexto(estruturas: dict[str, dict[str, Any]], estatisticas: dict[Any, Any], tenant: str | None) -> Contexto:
    relacoes = {(s, r["nome"]): r for s, doc in estruturas.items() for r in doc.get("relacoes", [])}
    return Contexto(relacoes, estatisticas, mapa_de_particoes(estruturas), tenant)


def carregar_contexto(doc: Path) -> Contexto:
    estruturas = carregar_estruturas(doc / "catalogo")
    return montar_contexto(estruturas, ler_estatisticas(doc / "catalogo"), coluna_de_tenant(doc))


def planejar(chave: Chave, ctx: Contexto) -> Plano | None:
    """Plano de amostragem do par, ou None se algum lado não permite SELECT."""
    origem, colunas_o, destino, colunas_d = chave
    rel_o, rel_d = ctx.relacoes.get(origem), ctx.relacoes.get(destino)
    if rel_o is None or rel_d is None or rel_o.get("acesso", "ok") != "ok" or rel_d.get("acesso", "ok") != "ok":
        return None
    linhas = linhas_estimadas(origem, rel_o.get("tipo") == "particionada", ctx.estatisticas, ctx.filhos)
    metodo, pct = calcular_pct(linhas)
    filtro = filtro_de_tenant(ctx.tenant, ctx.tipos_de(origem), ctx.tipos_de(destino), colunas_o, colunas_d)
    return Plano(metodo, pct, filtro)


# ----------------------------------------------------------------- verificação de um par


def _registro(
    chave: Chave, plano: Plano | None, situacao: str, amostrados: int, casados: int, erro: str | None, agora: str
) -> Registro:
    (so, ro), co, (sd, rd), cd = chave
    plano = plano or Plano(None, None, FILTRO_NENHUM)
    return {
        "origem": {"schema": so, "relacao": ro, "colunas": list(co)},
        "destino": {"schema": sd, "relacao": rd, "colunas": list(cd)},
        "versao_metodo": VERSAO_METODO,
        "metodo": plano.metodo,
        "pct": plano.pct,
        "filtro_tenant": plano.filtro_tenant,
        "amostrados": amostrados,
        "casados": casados,
        "taxa": round(casados / amostrados, 4) if amostrados > 0 else None,
        "situacao": situacao,
        "erro": erro,
        "verificado_em": agora,
    }


def _classe_do_erro(erro: Exception) -> str:
    """SQLSTATE quando houver; senão o nome da exceção. Nunca o texto do servidor."""
    return str(getattr(erro, "sqlstate", None) or type(erro).__name__)


def verificar_par(
    conexao: Any, chave: Chave, tipos_iguais: bool, ctx: Contexto, lim: Limiares, agora: str
) -> Registro:
    """Amostra o par e devolve o registro do cache (`verificado`, `sem_select`, `amostra_vazia` ou `erro`)."""
    plano = planejar(chave, ctx)
    if plano is None:
        return _registro(chave, None, "sem_select", 0, 0, None, agora)
    consulta = montar_consulta(chave, plano, tipos_iguais, ctx.tenant, lim.amostra_max)
    try:
        linhas = conexao.executar(consulta)
    except (ErroExecucao, SqlRejeitado) as erro:
        return _registro(chave, plano, "erro", 0, 0, _classe_do_erro(erro), agora)
    amostrados, casados = int(linhas[0][0]), int(linhas[0][1])
    situacao = "verificado" if amostrados > 0 else "amostra_vazia"
    return _registro(chave, plano, situacao, amostrados, casados, None, agora)


# ----------------------------------------------------------------- rodada


@dataclass
class ResultadoRodada:
    processados: int = 0
    consultas: int = 0
    segundos: float = 0.0
    situacoes: Counter[str] = field(default_factory=Counter)
    freio: str | None = None
    pendentes: int = 0


def _agora_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pendentes_de(pares: dict[Chave, bool], cache: dict[Chave, Registro], refazer: bool) -> list[Chave]:
    """Pares sem registro da versão atual do método (todos, com `refazer`), em ordem de chave."""
    return [
        k
        for k in sorted(pares)
        if refazer or k not in cache or cache[k]["versao_metodo"] != VERSAO_METODO
    ]


def _atualizar_sequencia(tipo: str | None, atual: str | None, sequencia: list[Chave], chave: Chave) -> tuple[str | None, list[Chave]]:
    """Sequência de falhas do mesmo tipo (timeout ou lock); qualquer outro resultado a zera."""
    if tipo is None:
        return None, []
    return tipo, (sequencia + [chave]) if tipo == atual else [chave]


def amostrar_pendentes(
    conexao: Any,
    ctx: Contexto,
    pares: dict[Chave, bool],
    cache: dict[Chave, Registro],
    lim: Limiares,
    salvar: Callable[[], None],
    *,
    limite: int | None = None,
    pausa_ms: int = 0,
    refazer: bool = False,
    agora: Callable[[], str] = _agora_iso,
    dormir: Callable[[float], None] = time.sleep,
    relogio: Callable[[], float] = time.monotonic,
) -> ResultadoRodada:
    """Amostra os pares pendentes, gravando o cache a cada 100 pares e ao sair (inclusive por interrupção)."""
    fila = pendentes_de(pares, cache, refazer)
    resultado = ResultadoRodada(pendentes=len(fila))
    if limite is not None:
        fila = fila[:limite]
    tipo_seq: str | None = None
    sequencia: list[Chave] = []
    try:
        for chave in fila:
            inicio = relogio()
            registro = verificar_par(conexao, chave, pares[chave], ctx, lim, agora())
            if registro["situacao"] != "sem_select":
                resultado.consultas += 1
                resultado.segundos += relogio() - inicio
                if pausa_ms > 0:
                    dormir(pausa_ms / 1000)
            cache[chave] = registro
            resultado.processados += 1
            resultado.situacoes[registro["situacao"]] += 1
            tipo = CODIGOS_DE_FREIO.get(str(registro["erro"])) if registro["situacao"] == "erro" else None
            tipo_seq, sequencia = _atualizar_sequencia(tipo, tipo_seq, sequencia, chave)
            if len(sequencia) >= FALHAS_SEGUIDAS_PARA_FREIO:
                _desfazer_sequencia(cache, sequencia, resultado)
                resultado.freio = tipo_seq
                break
            if resultado.processados % SALVAR_A_CADA == 0:
                salvar()
    finally:
        salvar()
    resultado.pendentes -= resultado.processados
    return resultado


def _desfazer_sequencia(cache: dict[Chave, Registro], sequencia: list[Chave], resultado: ResultadoRodada) -> None:
    """Os pares que dispararam o freio não entram no cache: serão tentados de novo na retomada."""
    for chave in sequencia:
        cache.pop(chave, None)
        resultado.situacoes["erro"] -= 1
        resultado.processados -= 1


# ----------------------------------------------------------------- grafo


def aplicar_ao_grafo(doc: Path, projeto: Projeto) -> dict[str, Any]:
    """Aplica o cache ao grafo (offline): confirma, rebaixa, descarta e desempata. Idempotente."""
    caminho = doc.joinpath(*SAIDA)
    if not caminho.is_file():
        raise ErroRelacionamentos(f"{caminho} não existe. Rode 'relacionamentos fks' antes.")
    existente = ler_grafo_existente(caminho)
    cache = carregar_cache(doc.joinpath(*SAIDA_CACHE))
    arestas, bloco = aplicar_cache(existente["arestas"], cache, limiares_do_projeto(projeto))
    resumo = existente["resumo"]
    grafo = montar_grafo(arestas, [], resumo.get("inferencia_nome"), resumo.get("inferencia_codigo"), bloco)
    validar(grafo)
    escrever_json(caminho, grafo)
    return grafo


def executar_verificacao(
    projeto: Projeto,
    *,
    refazer: bool = False,
    limite: int | None = None,
    pausa_ms: int = 0,
    conexao: Any = None,
    **injecoes: Any,
) -> ResultadoRodada:
    """Amostra os pares pendentes e grava o cache. Conecta só se houver par a consultar."""
    doc = projeto.doc
    existente = ler_grafo_existente(_caminho_grafo(doc))
    pares = coletar_pares(existente["arestas"])
    caminho_cache = doc.joinpath(*SAIDA_CACHE)
    cache = carregar_cache(caminho_cache)
    ctx = carregar_contexto(doc)
    lim = limiares_do_projeto(projeto)

    def salvar() -> None:
        gravar_cache(caminho_cache, cache)

    def rodar(c: Any) -> ResultadoRodada:
        return amostrar_pendentes(
            c, ctx, pares, cache, lim, salvar, limite=limite, pausa_ms=pausa_ms, refazer=refazer, **injecoes
        )

    if not pendentes_de(pares, cache, refazer) or limite == 0:
        return ResultadoRodada()
    if conexao is not None:
        return rodar(conexao)
    with Conexao(projeto) as aberta:
        return rodar(aberta)


def _caminho_grafo(doc: Path) -> Path:
    caminho = doc.joinpath(*SAIDA)
    if not caminho.is_file():
        raise ErroRelacionamentos(f"{caminho} não existe. Rode 'relacionamentos fks' antes.")
    return caminho


# ----------------------------------------------------------------- linha de comando


def _inteiro_nao_negativo(texto: str) -> int:
    try:
        valor = int(texto)
    except ValueError:
        raise argparse.ArgumentTypeError("deve ser um inteiro") from None
    if valor < 0:
        raise argparse.ArgumentTypeError("deve ser zero ou maior")
    return valor


def registrar(etapas: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = etapas.add_parser(
        "verificar",
        help="amostragem da taxa de casamento dos candidatos (conecta ao banco, só leitura); retoma do cache",
    )
    adicionar_argumentos_comuns(p)
    p.add_argument("--refazer", action="store_true", help="reamostra todos os pares, ignorando o cache")
    p.add_argument("--limite", type=_inteiro_nao_negativo, default=None, help="amostra no máximo N pares nesta execução")
    p.add_argument("--pausa-ms", type=_inteiro_nao_negativo, default=0, help="pausa entre consultas, em ms (padrão 0)")
    p.add_argument("--aplicar", action="store_true", help="só aplica o cache ao grafo, sem conectar ao banco")
    p.set_defaults(func=executar)


def imprimir_rodada(r: ResultadoRodada) -> None:
    media = r.segundos / r.consultas if r.consultas else 0.0
    situacoes = ", ".join(f"{s}={n}" for s, n in sorted(r.situacoes.items()) if n) or "nenhuma"
    print(
        f"Amostragem: pares verificados={r.processados}, consultas={r.consultas}, "
        f"tempo total={r.segundos:.1f} s, média por consulta={media:.3f} s; situações: {situacoes}; "
        f"pendentes={r.pendentes}."
    )


def imprimir_bloco(grafo: dict[str, Any]) -> None:
    b = grafo["resumo"]["verificacao"]
    print(
        f"Grafo: {grafo['resumo']['total']} arestas; pares={b['pares']}, verificados={b['verificados']}, "
        f"confirmadas={b['confirmadas']}, hipóteses={b['hipoteses']}, descartadas={b['descartadas']}, "
        f"sem SELECT={b['sem_select']}, amostra vazia={b['amostra_vazia']}, amostra pequena={b['amostra_pequena']}, "
        f"pendentes={b['pendentes']}, desempatadas={b['desempatadas']}, "
        f"ambíguas reduzidas={b['ambiguas_reduzidas']}, NOT VALID medidas={b['not_valid_medidas']}."
    )
    if b["erros"]:
        print("Erros por classe: " + ", ".join(f"{c}={n}" for c, n in b["erros"].items()) + ".")


def executar(args: argparse.Namespace) -> int:
    projeto = carregar_projeto(args.doc, args.workspace)
    if args.aplicar:
        imprimir_bloco(aplicar_ao_grafo(projeto.doc, projeto))
        return 0
    resultado = executar_verificacao(projeto, refazer=args.refazer, limite=args.limite, pausa_ms=args.pausa_ms)
    imprimir_rodada(resultado)
    if resultado.freio is not None:
        motivo = MENSAGEM_FREIO[resultado.freio].format(n=FALHAS_SEGUIDAS_PARA_FREIO)
        print(
            f"Parado: {motivo}, sinal de carga ou contenção no banco. O cache foi gravado; "
            "retome mais tarde, de preferência dentro da janela preferida.",
            file=sys.stderr,
        )
        return CODIGO_SAIDA_FREIO
    imprimir_bloco(aplicar_ao_grafo(projeto.doc, projeto))
    return 0
