"""Consulta de amostragem de um par candidato (decisão D-019, itens 2 e 6).

Offline até aqui: este módulo só decide o plano (método, `pct`, tenant) a partir do catálogo e monta
o texto do SQL. Quem executa é o comando `relacionamentos verificar`, sempre pela `Conexao`.

A consulta devolve só duas contagens (`amostrados` e `casados`): nenhum valor de dado sai do banco.
Identificadores vão por `psycopg.sql.Identifier`; os números (percentual, semente, limite) são
gerados aqui como literais numéricos, nunca a partir de texto do catálogo.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NamedTuple

from psycopg import sql

Relacao = tuple[str, str]
Chave = tuple[Relacao, tuple[str, ...], Relacao, tuple[str, ...]]

METODO_TABLESAMPLE = "tablesample"
METODO_DIRETO = "direto"
FILTRO_NENHUM = "nenhum"
FILTRO_CORRELACIONADO = "correlacionado"
LINHAS_ALVO = 5_000
LINHAS_MINIMAS_TABLESAMPLE = 50_000
PCT_MINIMO = 0.01
PCT_MAXIMO = 100.0
SEMENTE = 42


class Plano(NamedTuple):
    """Como o par será amostrado. `metodo` e `pct` são nulos quando o par não é consultado."""

    metodo: str | None
    pct: float | None
    filtro_tenant: str


# ----------------------------------------------------------------- estatísticas


def ler_estatisticas(raiz: Path) -> dict[Relacao, dict[str, Any]]:
    """`paginas` e `linhas_estimadas` por relação, de `<catalogo>/estatisticas/*.json` (se existirem)."""
    pasta = raiz / "estatisticas"
    resultado: dict[Relacao, dict[str, Any]] = {}
    for arquivo in sorted(pasta.glob("*.json")) if pasta.is_dir() else []:
        try:
            dados = json.loads(arquivo.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for rel in dados.get("relacoes", []) if isinstance(dados, dict) else []:
            resultado[(arquivo.stem, rel["nome"])] = {
                "paginas": rel.get("paginas"),
                "linhas_estimadas": rel.get("linhas_estimadas"),
            }
    return resultado


def mapa_de_particoes(estruturas: dict[str, dict[str, Any]]) -> dict[Relacao, list[Relacao]]:
    """Partições diretas de cada relação particionada, ordenadas."""
    filhos: dict[Relacao, list[Relacao]] = {}
    for schema, doc in estruturas.items():
        for rel in doc.get("relacoes", []):
            part = rel.get("particao")
            if isinstance(part, dict) and part.get("pai_nome"):
                pai = (part.get("pai_schema", schema), part["pai_nome"])
                filhos.setdefault(pai, []).append((schema, rel["nome"]))
    return {pai: sorted(lista) for pai, lista in filhos.items()}


def _linhas_proprias(estat: dict[str, Any] | None) -> int | None:
    """Linhas estimadas da relação, ou None se não há estatística utilizável (negativo, sem páginas)."""
    if not estat:
        return None
    linhas, paginas = estat.get("linhas_estimadas"), estat.get("paginas")
    if not isinstance(linhas, int) or not isinstance(paginas, int) or linhas < 0 or paginas <= 0:
        return None
    return linhas


def linhas_estimadas(
    relacao: Relacao,
    particionada: bool,
    estatisticas: dict[Relacao, dict[str, Any]],
    filhos: dict[Relacao, list[Relacao]],
    _visitadas: frozenset[Relacao] = frozenset(),
) -> int | None:
    """Linhas estimadas; para relação particionada, a soma das partições (recursiva). None: sem estatística."""
    if not particionada:
        return _linhas_proprias(estatisticas.get(relacao))
    if relacao in _visitadas:
        return None
    partes = [
        linhas_estimadas(f, f in filhos, estatisticas, filhos, _visitadas | {relacao})
        for f in filhos.get(relacao, [])
    ]
    validas = [p for p in partes if p is not None]
    return sum(validas) if validas else None


# ----------------------------------------------------------------- plano


def calcular_pct(linhas: int | None) -> tuple[str, float | None]:
    """`(metodo, pct)`: leitura direta para origem pequena ou sem estatística; senão TABLESAMPLE.

    O percentual mira cerca de `LINHAS_ALVO` linhas esperadas, limitado entre 0,01 e 100.
    """
    if linhas is None or linhas < LINHAS_MINIMAS_TABLESAMPLE:
        return METODO_DIRETO, None
    pct = 100.0 * LINHAS_ALVO / linhas
    return METODO_TABLESAMPLE, round(min(max(pct, PCT_MINIMO), PCT_MAXIMO), 4)


def filtro_de_tenant(
    tenant: str | None,
    origem: dict[str, str],
    destino: dict[str, str],
    colunas_origem: tuple[str, ...],
    colunas_destino: tuple[str, ...],
) -> str:
    """`correlacionado` se a coluna de tenant existe, com o mesmo tipo, nos dois lados e fora da chave."""
    if tenant is None or tenant not in origem or tenant not in destino:
        return FILTRO_NENHUM
    if origem[tenant] != destino[tenant] or tenant in colunas_origem or tenant in colunas_destino:
        return FILTRO_NENHUM
    return FILTRO_CORRELACIONADO


# ----------------------------------------------------------------- consulta


def _id(nome: str) -> sql.Identifier:
    return sql.Identifier(nome)


def montar_consulta(
    chave: Chave, plano: Plano, tipos_iguais: bool, tenant: str | None, amostra_max: int
) -> str:
    """Texto do SQL de amostragem do par (só contagens). `tenant` só é usado se o plano o correlaciona."""
    (so, ro), colunas_o, (sd, rd), colunas_d = chave
    usa_tenant = plano.filtro_tenant == FILTRO_CORRELACIONADO and tenant is not None
    cast = sql.SQL("" if tipos_iguais else "::text")
    selecao = [sql.SQL("f.{}{} AS {}").format(_id(c), cast, sql.SQL(f"v{i}")) for i, c in enumerate(colunas_o)]
    nao_nulos = [sql.SQL("f.{} IS NOT NULL").format(_id(c)) for c in colunas_o]
    iguais = [
        sql.SQL("p.{}{} = a.{}").format(_id(c), cast, sql.SQL(f"v{i}")) for i, c in enumerate(colunas_d)
    ]
    if usa_tenant and tenant is not None:
        selecao.append(sql.SQL("f.{} AS t").format(_id(tenant)))
        nao_nulos.append(sql.SQL("f.{} IS NOT NULL").format(_id(tenant)))
        iguais.append(sql.SQL("p.{} = a.t").format(_id(tenant)))
    amostragem = sql.SQL("")
    if plano.metodo == METODO_TABLESAMPLE and plano.pct is not None:
        amostragem = sql.SQL(" TABLESAMPLE SYSTEM ({}) REPEATABLE ({})").format(
            sql.SQL(f"{float(plano.pct):.4f}"), sql.SQL(str(int(SEMENTE)))
        )
    consulta = sql.SQL(
        "WITH amostra AS (SELECT {selecao} FROM {sch_o}.{rel_o} f{amostragem} WHERE {nao_nulos} LIMIT {limite}) "
        "SELECT count(*) AS amostrados, "
        "count(*) FILTER (WHERE EXISTS (SELECT 1 FROM {sch_d}.{rel_d} p WHERE {iguais})) AS casados "
        "FROM amostra a"
    ).format(
        selecao=sql.SQL(", ").join(selecao),
        sch_o=_id(so),
        rel_o=_id(ro),
        amostragem=amostragem,
        nao_nulos=sql.SQL(" AND ").join(nao_nulos),
        limite=sql.SQL(str(int(amostra_max))),
        sch_d=_id(sd),
        rel_d=_id(rd),
        iguais=sql.SQL(" AND ").join(iguais),
    )
    return consulta.as_string()


# ----------------------------------------------------------------- limiares

VERSAO_METODO = 1  # aumente se a forma da consulta mudar: registros antigos são reamostrados


class Limiares(NamedTuple):
    confirmado: float
    hipotese: float
    amostra_max: int
    amostra_min: int


def limiares_do_projeto(projeto: Any) -> Limiares:
    """Limiares de `execucao.verificacoes` do `projeto.yml`, com os padrões da D-019."""
    from pg_atlas.erros import ErroConfig

    lim = Limiares(
        projeto.taxa_confirmado, projeto.taxa_hipotese, projeto.amostra_max_valores, projeto.amostra_min_valores
    )
    if lim.hipotese > lim.confirmado:
        raise ErroConfig("projeto.yml: 'taxa_hipotese' não pode passar de 'taxa_confirmado'.")
    return lim
