-- Estatísticas por coluna de um schema, somente da view pg_stats e somente das colunas
-- permitidas (fração de nulos, distintos, largura média e correlação). Sem junções:
-- a ligação com o catálogo é feita em Python.
-- Parâmetro: %(schema)s (texto), um schema por vez.
-- Colunas: schemaname, tablename, attname, inherited, null_frac, n_distinct, avg_width,
-- correlation.
SELECT
    schemaname,
    tablename,
    attname,
    inherited,
    null_frac,
    n_distinct,
    avg_width,
    correlation
FROM pg_stats
WHERE schemaname = %(schema)s
  AND schemaname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND schemaname !~ '^pg_(toast_)?temp_'
ORDER BY schemaname, tablename, attname, inherited
