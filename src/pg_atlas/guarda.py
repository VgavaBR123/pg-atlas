"""Guarda de SQL: nada que possa escrever, administrar ou ler estatística proibida
chega ao banco.

Estratégia (na dúvida, rejeitar):

1. Um tokenizador próprio separa palavras, identificadores entre aspas, strings
   (simples, `E'..'`, dólar-quotadas) e comentários (inclusive aninhados). A
   varredura de palavras-chave olha só as palavras fora de strings e comentários,
   então `'DELETE'` ou `-- drop` não geram falso positivo e nada escapa escondido.
2. O comando precisa começar por SELECT, WITH, EXPLAIN ou SHOW; `;` só é aceito
   no fim.
3. Palavras de escrita (INSERT, UPDATE, DELETE, INTO, ...), `FOR UPDATE/SHARE`,
   `ANALYZE` e funções administrativas ou que executam SQL dinâmico são vetadas.
4. As colunas proibidas do `pg_stats` (valores mais comuns, histogramas) são
   vetadas em qualquer parte do texto, inclusive em strings (por causa de
   `query_to_xml('...')` e similares). Consultas ao `pg_stats` não podem usar `*`
   nem referência à linha inteira (`to_jsonb(s)`), o que exige análise com sqlglot.
5. Quando o sqlglot (dialeto postgres) consegue interpretar o comando, a árvore
   também é conferida (raiz deve ser consulta; sem nós de escrita).

A sessão também é aberta com `standard_conforming_strings = on` (ver conexao.py),
o que mantém o tokenizador alinhado com o analisador do servidor.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from pg_atlas.erros import SqlRejeitado

COLUNAS_ESTATISTICA_PROIBIDAS = (
    "most_common_vals",
    "most_common_freqs",
    "histogram_bounds",
    "most_common_elems",
    "most_common_elem_freqs",
    "elem_count_histogram",
)

# Tabelas de estatística que expõem valores reais e não têm colunas liberadas.
TABELAS_ESTATISTICA_PROIBIDAS = frozenset(
    {
        "pg_statistic", "pg_statistic_ext_data", "pg_stats_ext", "pg_stats_ext_exprs",
        # credenciais e texto de terceiros
        "pg_shadow", "pg_authid", "pg_user_mappings", "pg_user_mapping",
        "pg_hba_file_rules", "pg_file_settings", "pg_ident_file_mappings",
        # atividade com endereço de cliente ou texto de consultas alheias
        "pg_stat_get_activity", "pg_stat_replication", "pg_stat_wal_receiver", "pg_stat_ssl",
        "pg_stat_get_backend_activity", "pg_stat_get_backend_client_addr",
        "pg_stat_get_backend_client_port", "pg_stat_get_backend_userid",
    }
)  # fmt: skip

# Relações liberadas só com restrições: nome -> colunas permitidas (None = sem lista branca,
# valem apenas as colunas proibidas abaixo). Em ambas: sem '*', sem linha inteira e sem
# alias de tabela com lista de colunas (renomeação por posição burlaria as listas).
RELACOES_RESTRITAS: dict[str, frozenset[str] | None] = {
    "pg_stats": frozenset(
        {"schemaname", "tablename", "attname", "inherited", "null_frac", "avg_width",
         "n_distinct", "correlation"}
    ),
    "pg_stat_activity": None,
}  # fmt: skip

COLUNAS_PROIBIDAS_POR_RELACAO: dict[str, frozenset[str]] = {
    "pg_stat_activity": frozenset(
        {"query", "client_addr", "client_hostname", "client_port", "usename"}
    ),
}

PRIMEIRAS_PALAVRAS = frozenset({"select", "with", "explain", "show"})

PALAVRAS_PROIBIDAS = frozenset(
    {
        "insert", "update", "delete", "merge", "into", "drop", "create", "alter",
        "truncate", "grant", "revoke", "copy", "call", "do", "set", "reset", "lock",
        "vacuum", "analyze", "analyse", "refresh", "reindex", "cluster", "execute",
        "prepare", "declare", "listen", "notify",
    }
)  # fmt: skip

FUNCOES_PROIBIDAS = frozenset(
    {
        "set_config", "nextval", "setval", "currval", "lastval", "ts_stat",
        "lowrite", "loread", "pg_notify", "pg_promote", "pg_reload_conf",
        "pg_rotate_logfile", "pg_terminate_backend", "pg_cancel_backend",
        "pg_stat_file", "pg_largeobject", "pg_extension_config_dump",
        "inet_server_addr", "inet_server_port", "inet_client_addr", "inet_client_port",
        "pg_import_system_collations", "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    }
)  # fmt: skip

PREFIXOS_PROIBIDOS = (
    "dblink", "lo_", "pg_ls_", "pg_read_", "pg_file_", "pg_logical", "pg_replication_slot",
    "pg_advisory", "pg_stat_reset", "pg_stat_statements_reset", "pg_wal_replay",
    "pg_backup", "pg_create_", "pg_drop_", "pg_switch_", "pg_log_",
    "query_to_xml", "table_to_xml", "cursor_to_xml", "schema_to_xml", "database_to_xml",
)  # fmt: skip

_NOS_ESCRITA = tuple(
    tipo
    for nome in (
        "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "AlterTable",
        "Into", "Lock", "Set", "Copy", "Command", "TruncateTable", "Grant", "Revoke",
        "Use", "Transaction", "Commit", "Rollback", "Execute", "Declare",
    )
    if (tipo := getattr(exp, nome, None)) is not None
)  # fmt: skip

_RE_DOLAR = re.compile(r"\$([A-Za-z_\u0080-\U0010ffff][\w\u0080-\U0010ffff]*)?\$")

# (tipo, texto): w palavra, q identificador entre aspas, s string, n número, p símbolo
Token = tuple[str, str]


def _rejeitar(motivo: str) -> SqlRejeitado:
    return SqlRejeitado(f"SQL rejeitado pelo guarda: {motivo}")


def _inicio_palavra(c: str) -> bool:
    return c.isalpha() or c == "_" or ord(c) > 127


def _parte_palavra(c: str) -> bool:
    return c.isalnum() or c in "_$" or ord(c) > 127


def tokenizar(sql: str) -> list[Token]:
    """Divide o SQL em tokens, descartando comentários. Lança SqlRejeitado se malformado."""
    tokens: list[Token] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
        elif sql.startswith("--", i):
            fim = sql.find("\n", i)
            i = n if fim < 0 else fim + 1
        elif sql.startswith("/*", i):
            profundidade, i = 1, i + 2
            while profundidade:
                if i >= n:
                    raise _rejeitar("comentário de bloco sem fechamento")
                if sql.startswith("/*", i):
                    profundidade, i = profundidade + 1, i + 2
                elif sql.startswith("*/", i):
                    profundidade, i = profundidade - 1, i + 2
                else:
                    i += 1
        elif c == "'":
            i = _ler_string(sql, i, escapes=False)
            tokens.append(("s", ""))
        elif c == '"':
            if sql[max(0, i - 2) : i].lower() == "u&":
                raise _rejeitar('identificador com escape unicode (U&"...")')
            fim = i + 1
            conteudo: list[str] = []
            while True:
                if fim >= n:
                    raise _rejeitar("identificador entre aspas sem fechamento")
                if sql[fim] == '"':
                    if sql.startswith('""', fim):
                        conteudo.append('"')
                        fim += 2
                        continue
                    break
                conteudo.append(sql[fim])
                fim += 1
            tokens.append(("q", "".join(conteudo)))
            i = fim + 1
        elif c == "$" and (m := _RE_DOLAR.match(sql, i)):
            fim = sql.find(m.group(0), m.end())
            if fim < 0:
                raise _rejeitar("string dólar-quotada sem fechamento")
            tokens.append(("s", ""))
            i = fim + len(m.group(0))
        elif _inicio_palavra(c):
            fim = i + 1
            while fim < n and _parte_palavra(sql[fim]):
                fim += 1
            palavra = sql[i:fim]
            if palavra.lower() == "e" and fim < n and sql[fim] == "'":
                i = _ler_string(sql, fim, escapes=True)
                tokens.append(("s", ""))
            else:
                tokens.append(("w", palavra.lower()))
                i = fim
        elif c.isdigit():
            fim = i + 1
            while fim < n and (sql[fim].isalnum() or sql[fim] in "_."):
                fim += 1
            tokens.append(("n", sql[i:fim]))
            i = fim
        else:
            tokens.append(("p", c))
            i += 1
    return tokens


def _ler_string(sql: str, inicio: int, *, escapes: bool) -> int:
    """Devolve o índice após o fim da string que abre em `sql[inicio] == "'"`."""
    i, n = inicio + 1, len(sql)
    while i < n:
        c = sql[i]
        if escapes and c == "\\":
            i += 2
        elif c == "'":
            if sql.startswith("''", i):
                i += 2
            else:
                return i + 1
        else:
            i += 1
    raise _rejeitar("string sem fechamento")


def _funcao_proibida(nome: str) -> bool:
    return nome in FUNCOES_PROIBIDAS or nome.startswith(PREFIXOS_PROIBIDOS)


def _conferir_tokens(tokens: list[Token]) -> list[Token]:
    """Valida a forma geral e devolve os tokens sem o `;` final."""
    if tokens and tokens[-1] == ("p", ";"):
        tokens = tokens[:-1]
    if not tokens:
        raise _rejeitar("comando vazio")
    if ("p", ";") in tokens:
        raise _rejeitar("mais de um comando (';' intermediário)")
    tipo, primeira = tokens[0]
    if tipo != "w" or primeira not in PRIMEIRAS_PALAVRAS:
        raise _rejeitar("só são aceitos comandos que começam por SELECT, WITH, EXPLAIN ou SHOW")

    for indice, (tipo, texto) in enumerate(tokens):
        if tipo == "w":
            if texto in PALAVRAS_PROIBIDAS:
                raise _rejeitar(f"palavra proibida: {texto.upper()}")
            if texto == "for" and indice + 1 < len(tokens):
                seguinte = tokens[indice + 1]
                if seguinte[0] == "w" and seguinte[1] in {"update", "share", "no", "key"}:
                    raise _rejeitar("cláusula FOR UPDATE/SHARE")
        if tipo in ("w", "q"):
            nome = texto.lower()
            if _funcao_proibida(nome):
                raise _rejeitar(f"função ou objeto administrativo proibido: {nome}")
            if nome in TABELAS_ESTATISTICA_PROIBIDAS:
                raise _rejeitar(f"estatística com valores reais proibida: {nome}")

    if primeira == "explain":
        _conferir_explain(tokens)
    elif primeira == "show":
        if any(t != "w" and not (t == "p" and x == ".") for t, x in tokens[1:]):
            raise _rejeitar("SHOW aceita apenas o nome do parâmetro")
    return tokens


def _conferir_explain(tokens: list[Token]) -> None:
    i = 1
    if i < len(tokens) and tokens[i] == ("p", "("):
        profundidade = 0
        while i < len(tokens):
            if tokens[i] == ("p", "("):
                profundidade += 1
            elif tokens[i] == ("p", ")"):
                profundidade -= 1
                if profundidade == 0:
                    i += 1
                    break
            i += 1
        else:
            raise _rejeitar("opções do EXPLAIN sem fechamento")
    if i < len(tokens) and tokens[i] == ("w", "verbose"):
        i += 1
    if i >= len(tokens) or tokens[i][0] != "w" or tokens[i][1] not in {"select", "with"}:
        raise _rejeitar("EXPLAIN só aceita SELECT ou WITH")


def _conferir_arvore(sql: str, tokens: list[Token], restritas: list[str]) -> None:
    """Apoio do sqlglot. Se ele não entender e não houver relação restrita, vale a varredura de tokens."""
    if tokens[0][1] in {"explain", "show"}:
        return
    try:
        arvores = sqlglot.parse(sql, read="postgres")
    except SqlglotError:
        if restritas:
            raise _rejeitar("consulta a relação restrita que não pôde ser analisada") from None
        return
    arvores = [a for a in arvores if a is not None]
    if len(arvores) != 1:
        raise _rejeitar("mais de um comando")
    raiz = arvores[0]
    if not isinstance(raiz, exp.Query):
        raise _rejeitar("o comando não é uma consulta")
    for no in raiz.walk():
        if isinstance(no, _NOS_ESCRITA):
            raise _rejeitar(f"construção de escrita ou comando ({type(no).__name__})")
    if restritas:
        _conferir_relacoes_restritas(raiz, restritas)


def _conferir_relacoes_restritas(raiz: exp.Expression, restritas: list[str]) -> None:
    """Regras para pg_stats e pg_stat_activity (na dúvida, rejeitar)."""
    for estrela in raiz.find_all(exp.Star):
        if not isinstance(estrela.parent, exp.Count):
            raise _rejeitar("'*' não é permitido em consultas a relação restrita")
    nomes_de_tabela = set(restritas)
    for tabela in raiz.find_all(exp.Table):
        nomes_de_tabela.add(tabela.name.lower())
        if tabela.alias:
            nomes_de_tabela.add(tabela.alias.lower())
    for alias in raiz.find_all(exp.TableAlias):
        if alias.args.get("columns"):
            raise _rejeitar("alias de tabela com lista de colunas não é permitido")
        if alias.name:
            nomes_de_tabela.add(alias.name.lower())
    listas_brancas = [RELACOES_RESTRITAS[r] for r in restritas if RELACOES_RESTRITAS[r] is not None]
    proibidas: set[str] = set()
    for relacao in restritas:
        proibidas |= COLUNAS_PROIBIDAS_POR_RELACAO.get(relacao, frozenset())
    for coluna in raiz.find_all(exp.Column):
        nome = coluna.name.lower()
        if not coluna.table and nome in nomes_de_tabela:
            raise _rejeitar("referência à linha inteira de relação restrita não é permitida")
        if nome in proibidas:
            raise _rejeitar(f"coluna não permitida: {nome}")
        for lista in listas_brancas:
            if lista is not None and nome not in lista:
                raise _rejeitar(f"coluna fora da lista branca do pg_stats: {nome}")


def verificar(sql: str) -> None:
    """Lança `SqlRejeitado` se o SQL não puder ir ao banco; senão, retorna sem nada."""
    if not isinstance(sql, str):
        raise _rejeitar("o SQL deve ser texto")
    minusculo = sql.lower()
    for coluna in COLUNAS_ESTATISTICA_PROIBIDAS:
        if coluna in minusculo:
            raise _rejeitar(f"menção a coluna de estatística proibida: {coluna}")
    tokens = _conferir_tokens(tokenizar(sql))
    palavras = {x.lower() for t, x in tokens if t in ("w", "q")}
    restritas = sorted(r for r in RELACOES_RESTRITAS if r in palavras)
    if restritas:
        # colunas proibidas valem como palavra em qualquer lugar do comando
        for relacao in restritas:
            for coluna in COLUNAS_PROIBIDAS_POR_RELACAO.get(relacao, ()):
                if coluna in palavras:
                    raise _rejeitar(f"coluna não permitida: {coluna}")
    _conferir_arvore(sql, tokens, restritas)


def permitido(sql: str) -> tuple[bool, str | None]:
    """Versão sem exceção: `(True, None)` ou `(False, motivo)`."""
    try:
        verificar(sql)
    except SqlRejeitado as erro:
        return False, str(erro)
    return True, None
