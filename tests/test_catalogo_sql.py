"""Testes offline das consultas de catálogo (tarefa 1.1) e um teste marcado contra o banco."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from pg_atlas import catalogo_sql, guarda

ESPERADOS = sorted(
    [
        "schemas", "relacoes", "colunas", "restricoes", "indices", "views", "funcoes",
        "gatilhos", "sequencias", "tipos", "dependencias", "privilegios", "estatisticas",
        "estatisticas_colunas",
    ]
)  # fmt: skip

COLUNAS_ESPERADAS = {
    "schemas": ["schema", "dono", "comentario"],
    "relacoes": ["schema", "nome", "tipo", "dono", "comentario", "eh_particao", "pai_schema",
                 "pai_nome"],
    "colunas": ["schema", "relacao", "ordem", "nome", "tipo", "obrigatoria", "padrao",
                "identidade", "gerada", "comentario"],
    "restricoes": ["schema", "relacao", "nome", "tipo", "colunas", "ref_schema", "ref_relacao",
                   "ref_colunas", "acao_delete", "acao_update", "definicao"],
    "indices": ["schema", "relacao", "nome", "colunas", "unico", "parcial", "definicao"],
    "views": ["schema", "nome", "tipo", "definicao"],
    "funcoes": ["schema", "nome", "tipo", "linguagem", "argumentos", "retorno", "volatilidade",
                "security_definer", "codigo"],
    "gatilhos": ["schema", "relacao", "nome", "eventos", "momento", "funcao_nome", "definicao"],
    "sequencias": ["schema", "nome", "tipo", "coluna_relacao", "coluna_nome"],
    "tipos": ["schema", "nome", "categoria", "valores"],
    "dependencias": ["schema", "view", "ref_schema", "ref_relacao"],
    "privilegios": ["schema", "classe", "nome", "tem_select", "tem_execute", "acesso"],
    "estatisticas": ["schema", "relacao", "linhas_estimadas", "tamanho_total_bytes",
                     "ultimo_analyze", "seq_scans", "idx_scans"],
    "estatisticas_colunas": ["schemaname", "tablename", "attname", "null_frac", "n_distinct",
                             "avg_width", "correlation"],
}  # fmt: skip

TABELAS_DE_USUARIO_PROIBIDAS = re.compile(r"\b(count\s*\(|public\.)", re.IGNORECASE)


def test_lista_exata() -> None:
    assert catalogo_sql.listar() == ESPERADOS
    assert sorted(COLUNAS_ESPERADAS) == ESPERADOS


def test_carregar_inexistente_e_invalido() -> None:
    with pytest.raises(FileNotFoundError):
        catalogo_sql.carregar("nao_existe")
    with pytest.raises(ValueError):
        catalogo_sql.carregar("../conexao")


@pytest.mark.parametrize("nome", ESPERADOS)
def test_passa_no_guarda(nome: str) -> None:
    guarda.verificar(catalogo_sql.carregar(nome))


@pytest.mark.parametrize("nome", ESPERADOS)
def test_sqlglot_interpreta(nome: str) -> None:
    arvore = sqlglot.parse_one(catalogo_sql.carregar(nome), read="postgres")
    assert isinstance(arvore, exp.Query)


@pytest.mark.parametrize("nome", ESPERADOS)
def test_parametro_e_exclusoes(nome: str) -> None:
    sql = catalogo_sql.carregar(nome)
    assert "%(schema)s" in sql
    for excluido in ("pg_catalog", "information_schema", "pg_toast"):
        assert f"'{excluido}'" in sql
    assert "pg_(toast_)?temp_" in sql


@pytest.mark.parametrize("nome", ESPERADOS)
def test_order_by_e_cabecalho(nome: str) -> None:
    sql = catalogo_sql.carregar(nome)
    assert sql.startswith("--")
    assert re.search(r"\bORDER BY\b", sql)
    assert not sql.rstrip().endswith(";")
    assert "\r" not in sql


@pytest.mark.parametrize("nome", ESPERADOS)
def test_sem_colunas_proibidas_nem_tabelas_de_usuario(nome: str) -> None:
    sql = catalogo_sql.carregar(nome).lower()
    for coluna in guarda.COLUNAS_ESTATISTICA_PROIBIDAS:
        assert coluna not in sql
    assert not TABELAS_DE_USUARIO_PROIBIDAS.search(sql)
    arvore = sqlglot.parse_one(sql, read="postgres")
    for tabela in arvore.find_all(exp.Table):
        assert tabela.name.startswith(("pg_", "unnest", "generate_series")) or not tabela.name


def test_estatisticas_colunas_so_pg_stats_e_lista_branca() -> None:
    arvore = sqlglot.parse_one(catalogo_sql.carregar("estatisticas_colunas"), read="postgres")
    assert [t.name for t in arvore.find_all(exp.Table)] == ["pg_stats"]
    assert not list(arvore.find_all(exp.Join))
    lista = guarda.RELACOES_RESTRITAS["pg_stats"]
    assert lista is not None
    assert {c.name for c in arvore.find_all(exp.Column)} <= lista


def test_parametros() -> None:
    assert catalogo_sql.parametros("x") == {"schema": "x"}


@pytest.mark.banco
def test_consultas_contra_o_banco_real() -> None:
    """Executa cada consulta no banco real (uma conexão, pelo pg_atlas). Só confere colunas."""
    from pg_atlas.config import carregar_projeto
    from pg_atlas.conexao import Conexao

    pasta = os.environ.get("PG_ATLAS_DOC_REAL")
    if not pasta or not Path(pasta).is_dir():
        pytest.skip("defina PG_ATLAS_DOC_REAL com a pasta do repositório de documentação")
    projeto = carregar_projeto(pasta)
    with Conexao(projeto) as conexao:
        _, linhas = conexao.executar_com_colunas(
            catalogo_sql.carregar("schemas"), catalogo_sql.parametros(None)
        )
        assert linhas, "nenhum schema visível"
        schema = linhas[0][0]
        for nome in ESPERADOS:
            colunas, resultado = conexao.executar_com_colunas(
                catalogo_sql.carregar(nome), catalogo_sql.parametros(schema)
            )
            faltando = [c for c in COLUNAS_ESPERADAS[nome] if c not in colunas]
            assert not faltando, f"{nome}: faltam colunas {faltando}"
            print(f"{nome}: {len(colunas)} colunas, {len(resultado)} linhas")
