-- Definição (via pg_views e pg_matviews) das views e materializadas de um schema.
-- Parâmetro: %(schema)s (texto), um schema por vez.
-- Relações de extensões (pg_depend, deptype 'e') ficam de fora.
-- Colunas: schema, nome, tipo ('v' view, 'm' materializada), dono, populada
-- (só materializadas), definicao.
SELECT
    v.schemaname AS schema,
    v.viewname AS nome,
    'v' AS tipo,
    v.viewowner AS dono,
    NULL::boolean AS populada,
    v.definition AS definicao
FROM pg_views AS v
JOIN pg_namespace AS n ON n.nspname = v.schemaname
JOIN pg_class AS c ON c.relnamespace = n.oid AND c.relname = v.viewname
WHERE v.schemaname = %(schema)s
  AND v.schemaname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND v.schemaname !~ '^pg_(toast_)?temp_'
  AND NOT EXISTS (
      SELECT 1 FROM pg_depend AS x
      WHERE x.classid = 'pg_class'::regclass AND x.objid = c.oid AND x.deptype = 'e'
  )
UNION ALL
SELECT
    m.schemaname AS schema,
    m.matviewname AS nome,
    'm' AS tipo,
    m.matviewowner AS dono,
    m.ispopulated AS populada,
    m.definition AS definicao
FROM pg_matviews AS m
JOIN pg_namespace AS n ON n.nspname = m.schemaname
JOIN pg_class AS c ON c.relnamespace = n.oid AND c.relname = m.matviewname
WHERE m.schemaname = %(schema)s
  AND m.schemaname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND m.schemaname !~ '^pg_(toast_)?temp_'
  AND NOT EXISTS (
      SELECT 1 FROM pg_depend AS x
      WHERE x.classid = 'pg_class'::regclass AND x.objid = c.oid AND x.deptype = 'e'
  )
ORDER BY schema, nome
