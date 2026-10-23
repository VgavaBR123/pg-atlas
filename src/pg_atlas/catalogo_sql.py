"""Acesso às consultas de catálogo (arquivos `.sql` em `pg_atlas/sql/catalogo/`)."""

from __future__ import annotations

from importlib import resources

PACOTE_SQL = "pg_atlas.sql.catalogo"
PARAMETRO_SCHEMA = "schema"


def listar() -> list[str]:
    """Nomes (sem extensão) das consultas de catálogo, em ordem alfabética."""
    pasta = resources.files(PACOTE_SQL)
    return sorted(p.name[: -len(".sql")] for p in pasta.iterdir() if p.name.endswith(".sql"))


def carregar(nome: str) -> str:
    """Texto da consulta `nome` (sem `.sql`). Lança FileNotFoundError se não existir."""
    if "/" in nome or "\\" in nome or nome.startswith("."):
        raise ValueError(f"nome de consulta inválido: {nome!r}")
    arquivo = resources.files(PACOTE_SQL) / f"{nome}.sql"
    if not arquivo.is_file():
        raise FileNotFoundError(f"consulta de catálogo inexistente: {nome}")
    return arquivo.read_text(encoding="utf-8")


def parametros(schema: str | None) -> dict[str, str | None]:
    """Parâmetros nomeados do psycopg para qualquer consulta de catálogo."""
    return {PARAMETRO_SCHEMA: schema}
