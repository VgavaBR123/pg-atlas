-- Gatilhos não internos das tabelas de um schema.
-- Parâmetro: %(schema)s (texto), um schema por vez (schema da tabela do gatilho).
-- Relações de extensões (pg_depend, deptype 'e') ficam de fora.
-- Colunas: schema, relacao, nome, eventos (INSERT, UPDATE, DELETE, TRUNCATE),
-- momento (BEFORE, AFTER, INSTEAD OF), nivel (ROW ou STATEMENT), habilitado,
-- funcao_schema, funcao_nome, definicao (pg_get_triggerdef).
SELECT
    n.nspname AS schema,
    c.relname AS relacao,
    t.tgname AS nome,
    array_remove(ARRAY[
        CASE WHEN t.tgtype & 4 <> 0 THEN 'INSERT' END,
        CASE WHEN t.tgtype & 8 <> 0 THEN 'DELETE' END,
        CASE WHEN t.tgtype & 16 <> 0 THEN 'UPDATE' END,
        CASE WHEN t.tgtype & 32 <> 0 THEN 'TRUNCATE' END
    ], NULL) AS eventos,
    CASE
        WHEN t.tgtype & 64 <> 0 THEN 'INSTEAD OF'
        WHEN t.tgtype & 2 <> 0 THEN 'BEFORE'
        ELSE 'AFTER'
    END AS momento,
    CASE WHEN t.tgtype & 1 <> 0 THEN 'ROW' ELSE 'STATEMENT' END AS nivel,
    t.tgenabled::text AS habilitado,
    pn.nspname AS funcao_schema,
    p.proname AS funcao_nome,
    pg_get_triggerdef(t.oid) AS definicao
FROM pg_trigger AS t
JOIN pg_class AS c ON c.oid = t.tgrelid
JOIN pg_namespace AS n ON n.oid = c.relnamespace
JOIN pg_proc AS p ON p.oid = t.tgfoid
JOIN pg_namespace AS pn ON pn.oid = p.pronamespace
WHERE NOT t.tgisinternal
  AND n.nspname = %(schema)s
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND n.nspname !~ '^pg_(toast_)?temp_'
  AND NOT EXISTS (
      SELECT 1 FROM pg_depend AS x
      WHERE x.classid = 'pg_class'::regclass AND x.objid = c.oid AND x.deptype = 'e'
  )
ORDER BY n.nspname, c.relname, t.tgname
