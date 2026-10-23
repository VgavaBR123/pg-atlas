"""Comando `diagnostico`: confere a conexão sem ler nenhum dado de tabela.

Não exibe host, usuário, banco nem senha: da role, só mostra se é superusuária e o
limite de conexões.
"""

from __future__ import annotations

import argparse

from pg_atlas.cli import adicionar_argumentos_comuns
from pg_atlas.config import carregar_projeto
from pg_atlas.conexao import Conexao, verificar_credenciais
from pg_atlas.erros import ErroCredenciais

SQL_VERSAO = "SELECT version()"
SQL_ROLE = "SELECT rolsuper, rolconnlimit FROM pg_roles WHERE rolname = current_user"
SQL_SOMENTE_LEITURA = (
    "SELECT current_setting('transaction_read_only'), "
    "current_setting('default_transaction_read_only')"
)
SQL_SCHEMAS = (
    "SELECT count(*) AS total, "
    "count(*) FILTER (WHERE has_schema_privilege(oid, 'USAGE')) AS com_uso "
    "FROM pg_namespace "
    "WHERE nspname <> 'information_schema' AND nspname !~ '^pg_'"
)
SQL_CONEXOES_EM_USO = (
    "SELECT count(*) FROM pg_stat_activity "
    "WHERE usesysid = (SELECT oid FROM pg_roles WHERE rolname = current_user)"
)


def registrar(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "diagnostico", help="testa a conexão e mostra o estado da sessão (somente leitura)"
    )
    adicionar_argumentos_comuns(parser)
    parser.set_defaults(func=executar)


def _limite(valor: int) -> str:
    return "ilimitado" if valor < 0 else str(valor)


def executar(args: argparse.Namespace) -> int:
    projeto = carregar_projeto(args.doc, args.workspace)
    credenciais = verificar_credenciais(projeto.workspace)
    if not credenciais.ok:
        raise ErroCredenciais(credenciais.mensagem())

    with Conexao(projeto) as conexao:
        versao = conexao.executar(SQL_VERSAO)[0][0]
        superusuaria, limite = conexao.executar(SQL_ROLE)[0]
        tx_somente_leitura, padrao_somente_leitura = conexao.executar(SQL_SOMENTE_LEITURA)[0]
        total_schemas, schemas_com_uso = conexao.executar(SQL_SCHEMAS)[0]
        em_uso = conexao.executar(SQL_CONEXOES_EM_USO)[0][0]
        timeout_ms = conexao.statement_timeout_ms

    somente_leitura = tx_somente_leitura == "on" and padrao_somente_leitura == "on"
    print(f"Projeto: {projeto.nome}")
    print(f"Versão do PostgreSQL: {versao}")
    print(f"Sessão somente leitura: {'sim' if somente_leitura else 'NÃO'}")
    print(f"Role é superusuária: {'sim' if superusuaria else 'não'}")
    print(f"Schemas (exceto de sistema): {total_schemas}, com permissão de uso: {schemas_com_uso}")
    print(f"Limite de conexões da role: {_limite(limite)}")
    print(f"Conexões em uso pela role: {em_uso}")
    print(f"statement_timeout: {timeout_ms} ms")
    return 0 if somente_leitura else 6
