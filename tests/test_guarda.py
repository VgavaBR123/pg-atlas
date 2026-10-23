"""Guarda de SQL: o que passa e o que nunca pode chegar ao banco."""

from __future__ import annotations

import pytest

from pg_atlas.erros import SqlRejeitado
from pg_atlas.guarda import permitido, verificar

COLS = ", ".join(f"c{i}" for i in range(1, 17))

ACEITOS = [
    "SELECT 1",
    "select 1;",
    "  \n\t SELECT 1  ;  ",
    "-- comentário inicial\nSELECT 1",
    "/* bloco /* aninhado */ ainda comentário */ SELECT 1",
    "WITH a AS (SELECT 1 AS x) SELECT x FROM a",
    "EXPLAIN SELECT 1",
    "EXPLAIN (FORMAT JSON) SELECT 1",
    "EXPLAIN (COSTS off) WITH a AS (SELECT 1) SELECT * FROM a",
    "SHOW server_version",
    "SHOW default_transaction_read_only;",
    # palavras perigosas apenas dentro de strings ou comentários: sem falso positivo
    "SELECT 'DELETE FROM x; DROP TABLE y' AS texto",
    "SELECT 1 -- ; DROP TABLE x",
    "SELECT 1 /* UPDATE x SET a = 1; */",
    "SELECT $$ insert into x; update y $$",
    "SELECT $corpo$ copy x to stdout; $corpo$ AS t",
    "SELECT E'it\\'s; delete' AS t",
    "SELECT 'it''s; drop' AS t",
    "SELECT 'for update' AS t",
    'SELECT 1 AS "update"',
    "SELECT last_analyze, n_live_tup FROM pg_stat_user_tables",
    "SELECT relname, reltuples FROM pg_class WHERE relkind = 'r' ORDER BY 1",
    "SELECT substring('abc' FROM 1 FOR 2)",
    "SELECT count(*) FROM pg_stats",
    "SELECT attname, null_frac, n_distinct, avg_width, correlation FROM pg_stats WHERE tablename = 'x'",
    "SELECT s.attname, s.null_frac FROM pg_stats AS s",
    "SELECT schemaname, tablename, attname, inherited, avg_width, n_distinct FROM pg_catalog.pg_stats",
    "SELECT count(*) FROM pg_stat_activity",
    "SELECT state, count(*) FROM pg_stat_activity GROUP BY state",
    "SELECT count(*) FROM pg_stat_activity WHERE usesysid = (SELECT oid FROM pg_roles WHERE rolname = current_user)",
    "SELECT pg_get_functiondef(p.oid) FROM pg_proc p LIMIT 1",
    "SELECT has_table_privilege(c.oid, 'SELECT') FROM pg_class c LIMIT 1",
    "SELECT * FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname <> %s",
]

REJEITADOS = [
    # comandos que não são consulta
    "UPDATE t SET a = 1",
    "DELETE FROM t",
    "INSERT INTO t VALUES (1)",
    "CREATE TABLE t (a int)",
    "DROP TABLE t",
    "ALTER TABLE t ADD COLUMN b int",
    "TRUNCATE t",
    "GRANT SELECT ON t TO alguem",
    "REVOKE SELECT ON t FROM alguem",
    "COPY t TO STDOUT",
    "CALL procedimento()",
    "DO $$ BEGIN PERFORM 1; END $$",
    "SET default_transaction_read_only = off",
    "RESET ALL",
    "BEGIN",
    "COMMIT",
    "VACUUM t",
    "LOCK TABLE t",
    "VALUES (1)",
    "(SELECT 1)",
    "",
    "   ",
    "-- só comentário",
    "/* só comentário */",
    ";",
    "/* disfarce */ DELETE FROM t",
    "-- ok\nUPDATE t SET a = 1",
    # vários comandos
    "SELECT 1; SELECT 2",
    "SELECT 1; DROP TABLE t",
    "SELECT 1;; ",
    "SHOW ALL; DROP TABLE t",
    # fugas
    "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",
    "WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x",
    "WITH x AS (UPDATE t SET a = 1 RETURNING *) SELECT * FROM x",
    "SELECT * INTO nova_tabela FROM t",
    "SELECT 1 INTO TEMP nova",
    "EXPLAIN ANALYZE DELETE FROM t",
    "EXPLAIN ANALYZE SELECT 1",
    "EXPLAIN (ANALYZE) SELECT 1",
    "EXPLAIN (ANALYZE true, BUFFERS) SELECT 1",
    "EXPLAIN DELETE FROM t",
    "EXPLAIN INSERT INTO t VALUES (1)",
    "EXPLAIN CREATE TABLE x AS SELECT 1",
    "SELECT * FROM t FOR UPDATE",
    "SELECT * FROM t FOR SHARE",
    "SELECT * FROM t FOR NO KEY UPDATE",
    "SELECT * FROM t FOR KEY SHARE",
    "SELECT * FROM t FOR UPDATE SKIP LOCKED",
    "SHOW search_path, 1",
    # funções administrativas e de execução dinâmica
    "SELECT pg_terminate_backend(1)",
    "SELECT pg_cancel_backend(1)",
    "SELECT set_config('default_transaction_read_only', 'off', false)",
    "SELECT lo_import('/etc/passwd')",
    "SELECT lo_export(1, '/tmp/x')",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT pg_read_binary_file('/etc/passwd')",
    "SELECT pg_ls_dir('.')",
    "SELECT * FROM dblink('host=x', 'select 1') AS t(a int)",
    "SELECT dblink_exec('x', 'drop table t')",
    "SELECT nextval('seq')",
    "SELECT setval('seq', 1)",
    "SELECT pg_sleep(100)",
    "SELECT pg_advisory_lock(1)",
    "SELECT pg_reload_conf()",
    "SELECT pg_notify('canal', 'x')",
    "SELECT query_to_xml('select 1', true, true, '')",
    "SELECT * FROM ts_stat('select 1')",
    'SELECT "pg_terminate_backend"(1)',
    "SELECT pg_catalog.\"set_config\"('a', 'b', false)",
    'SELECT U&"pg_terminate_backend"(1)',
    "SELECT PG_TERMINATE_BACKEND(1)",
    # estatísticas proibidas
    "SELECT most_common_vals FROM pg_stats",
    "SELECT MOST_COMMON_VALS FROM pg_stats",
    "SELECT most_common_freqs FROM pg_stats",
    "SELECT histogram_bounds FROM pg_stats",
    "SELECT most_common_elems FROM pg_stats",
    "SELECT most_common_elem_freqs FROM pg_stats",
    "SELECT elem_count_histogram FROM pg_stats",
    'SELECT "most_common_vals" FROM pg_stats',
    "SELECT 1 -- most_common_vals",
    "SELECT 1 /* histogram_bounds */",
    "SELECT query_to_xml('select most_common_vals from pg_stats', true, true, '')",
    "SELECT * FROM pg_stats",
    "SELECT s.* FROM pg_stats s",
    "SELECT to_jsonb(s) FROM pg_stats s",
    "SELECT row_to_json(pg_stats) FROM pg_stats",
    "WITH e AS (SELECT * FROM pg_stats) SELECT attname FROM e",
    "SELECT * FROM pg_statistic",
    "SELECT stavalues1 FROM pg_catalog.pg_statistic",
    "SELECT * FROM pg_stats_ext",
    "SELECT * FROM pg_stats_ext_exprs",
    # renomeação por posição e colunas fora da lista branca
    "SELECT c8 FROM pg_stats AS x(%s)" % COLS,
    "SELECT c8 FROM pg_stats x(%s)" % COLS,
    "SELECT c8 FROM pg_catalog.pg_stats x(%s)" % COLS,
    "SELECT c8 FROM pg_catalog.pg_stats AS x(%s)" % COLS,
    "SELECT c1 FROM (SELECT 1) AS y(c1), pg_stats",
    "SELECT tablespace FROM pg_stats",
    "SELECT attname AS apelido FROM pg_stats ORDER BY apelido",
    "SELECT usesysid FROM pg_stat_activity a(x, y)",
    # credenciais, texto de terceiros e endereços
    "SELECT * FROM pg_shadow",
    "SELECT rolpassword FROM pg_authid",
    "SELECT umoptions FROM pg_user_mappings",
    "SELECT * FROM pg_hba_file_rules",
    "SELECT * FROM pg_file_settings",
    "SELECT * FROM pg_ident_file_mappings",
    "SELECT inet_server_addr()",
    "SELECT inet_server_port()",
    "SELECT inet_client_addr()",
    "SELECT inet_client_port()",
    "SELECT * FROM pg_stat_get_activity(NULL)",
    "SELECT pg_stat_get_backend_activity(1)",
    "SELECT pg_stat_get_backend_client_addr(1)",
    "SELECT * FROM pg_stat_replication",
    # pg_stat_activity: só uso agregado
    "SELECT query FROM pg_stat_activity",
    "SELECT client_addr FROM pg_stat_activity",
    "SELECT client_hostname FROM pg_stat_activity",
    "SELECT client_port FROM pg_stat_activity",
    "SELECT usename FROM pg_stat_activity",
    "SELECT count(*) FROM pg_stat_activity WHERE usename = current_user",
    "SELECT * FROM pg_stat_activity",
    "SELECT a.* FROM pg_stat_activity a",
    "SELECT to_jsonb(a) FROM pg_stat_activity a",
    "SELECT row_to_json(pg_stat_activity) FROM pg_stat_activity",
    # malformados
    "SELECT 'sem fim",
    "SELECT 1 /* sem fim",
    'SELECT "sem fim',
    "SELECT $$ sem fim",
]


@pytest.mark.parametrize("sql", ACEITOS)
def test_aceita(sql: str) -> None:
    verificar(sql)
    assert permitido(sql) == (True, None)


@pytest.mark.parametrize("sql", REJEITADOS)
def test_rejeita(sql: str) -> None:
    with pytest.raises(SqlRejeitado):
        verificar(sql)
    ok, motivo = permitido(sql)
    assert not ok and motivo


def test_motivo_nao_repete_o_sql() -> None:
    _, motivo = permitido("SELECT pg_terminate_backend(12345678)")
    assert "12345678" not in motivo
