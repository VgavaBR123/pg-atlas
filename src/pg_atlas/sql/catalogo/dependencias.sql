-- De quais relações cada view (ou materializada) de um schema depende.
-- Parâmetro: %(schema)s (texto), um schema por vez (schema da view).
-- Origem: pg_rewrite (regra da view) e pg_depend. Views de extensões ficam de fora.
-- As relações referenciadas podem estar em qualquer schema.
-- Colunas: schema, view, tipo_view, ref_schema, ref_relacao, ref_tipo (relkind).
SELECT DISTINCT
    n.nspname AS schema,
    v.relname AS view,
    v.relkind::text AS tipo_view,
    rn.nspname AS ref_schema,
    rc.relname AS ref_relacao,
    rc.relkind::text AS ref_tipo
FROM pg_rewrite AS r
JOIN pg_class AS v ON v.oid = r.ev_class
JOIN pg_namespace AS n ON n.oid = v.relnamespace
JOIN pg_depend AS d
    ON d.classid = 'pg_rewrite'::regclass
   AND d.objid = r.oid
   AND d.refclassid = 'pg_class'::regclass
JOIN pg_class AS rc ON rc.oid = d.refobjid
JOIN pg_namespace AS rn ON rn.oid = rc.relnamespace
WHERE n.nspname = %(schema)s
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND n.nspname !~ '^pg_(toast_)?temp_'
  AND v.relkind IN ('v', 'm')
  AND rc.oid <> v.oid
  AND NOT EXISTS (
      SELECT 1 FROM pg_depend AS x
      WHERE x.classid = 'pg_class'::regclass AND x.objid = v.oid AND x.deptype = 'e'
  )
ORDER BY schema, view, ref_schema, ref_relacao
