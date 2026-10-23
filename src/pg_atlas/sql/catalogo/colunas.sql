-- Colunas das relações de um schema (r, p, v, m, f), sem colunas removidas.
-- Parâmetro: %(schema)s (texto), um schema por vez.
-- Relações de extensões (pg_depend, deptype 'e') ficam de fora.
-- Colunas: schema, relacao, ordem, nome, tipo, obrigatoria, padrao, identidade
-- ('a' sempre, 'd' por padrão, vazio se não é identidade), gerada ('s' armazenada,
-- vazio se não), expressao_gerada, comentario.
SELECT
    n.nspname AS schema,
    c.relname AS relacao,
    a.attnum::int AS ordem,
    a.attname AS nome,
    format_type(a.atttypid, a.atttypmod) AS tipo,
    a.attnotnull AS obrigatoria,
    CASE WHEN a.attgenerated = '' THEN pg_get_expr(d.adbin, d.adrelid) END AS padrao,
    a.attidentity::text AS identidade,
    a.attgenerated::text AS gerada,
    CASE WHEN a.attgenerated <> '' THEN pg_get_expr(d.adbin, d.adrelid) END AS expressao_gerada,
    col_description(c.oid, a.attnum) AS comentario
FROM pg_attribute AS a
JOIN pg_class AS c ON c.oid = a.attrelid
JOIN pg_namespace AS n ON n.oid = c.relnamespace
LEFT JOIN pg_attrdef AS d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE n.nspname = %(schema)s
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND n.nspname !~ '^pg_(toast_)?temp_'
  AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND a.attnum > 0
  AND NOT a.attisdropped
  AND NOT EXISTS (
      SELECT 1 FROM pg_depend AS x
      WHERE x.classid = 'pg_class'::regclass AND x.objid = c.oid AND x.deptype = 'e'
  )
ORDER BY n.nspname, c.relname, a.attnum
