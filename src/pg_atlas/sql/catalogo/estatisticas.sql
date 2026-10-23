-- Estatísticas de uso e tamanho das tabelas de um schema (só catálogo e views de estatística).
-- Parâmetro: %(schema)s (texto), um schema por vez.
-- Relações de extensões (pg_depend, deptype 'e') ficam de fora.
-- Contagem de linhas vem de pg_class.reltuples (estimativa; -1 = nunca analisada).
-- Colunas: schema, relacao, tipo, linhas_estimadas, paginas, tamanho_total_bytes,
-- tamanho_tabela_bytes, ultimo_analyze, ultimo_autoanalyze, ultimo_vacuum,
-- ultimo_autovacuum, seq_scans, linhas_lidas_seq, idx_scans, linhas_vivas, linhas_mortas.
-- Os campos de pg_stat_user_tables ficam nulos para relações que ela não cobre.
SELECT
    n.nspname AS schema,
    c.relname AS relacao,
    c.relkind::text AS tipo,
    c.reltuples::bigint AS linhas_estimadas,
    c.relpages::bigint AS paginas,
    pg_total_relation_size(c.oid) AS tamanho_total_bytes,
    pg_relation_size(c.oid) AS tamanho_tabela_bytes,
    st.last_analyze AS ultimo_analyze,
    st.last_autoanalyze AS ultimo_autoanalyze,
    st.last_vacuum AS ultimo_vacuum,
    st.last_autovacuum AS ultimo_autovacuum,
    st.seq_scan AS seq_scans,
    st.seq_tup_read AS linhas_lidas_seq,
    st.idx_scan AS idx_scans,
    st.n_live_tup AS linhas_vivas,
    st.n_dead_tup AS linhas_mortas
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
LEFT JOIN pg_stat_user_tables AS st ON st.relid = c.oid
WHERE n.nspname = %(schema)s
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND n.nspname !~ '^pg_(toast_)?temp_'
  AND c.relkind IN ('r', 'p', 'm')
  AND NOT EXISTS (
      SELECT 1 FROM pg_depend AS x
      WHERE x.classid = 'pg_class'::regclass AND x.objid = c.oid AND x.deptype = 'e'
  )
ORDER BY n.nspname, c.relname
