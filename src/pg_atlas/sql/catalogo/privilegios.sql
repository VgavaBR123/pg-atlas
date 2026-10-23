-- Privilégios da credencial atual sobre as relações e rotinas de um schema.
-- Parâmetro: %(schema)s (texto), um schema por vez.
-- Relações e rotinas de extensões (pg_depend, deptype 'e') ficam de fora.
-- Colunas: schema, classe ('relacao' ou 'funcao'), nome, tipo (relkind ou prokind),
-- argumentos (só rotinas), tem_select (só relações), tem_execute (só rotinas),
-- acesso ('sem_select' quando a relação não pode ser lida; 'ok' nos demais casos).
SELECT
    n.nspname AS schema,
    'relacao' AS classe,
    c.relname AS nome,
    c.relkind::text AS tipo,
    NULL::text AS argumentos,
    has_table_privilege(c.oid, 'SELECT') AS tem_select,
    NULL::boolean AS tem_execute,
    CASE WHEN has_table_privilege(c.oid, 'SELECT') THEN 'ok' ELSE 'sem_select' END AS acesso
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = %(schema)s
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND n.nspname !~ '^pg_(toast_)?temp_'
  AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND NOT EXISTS (
      SELECT 1 FROM pg_depend AS x
      WHERE x.classid = 'pg_class'::regclass AND x.objid = c.oid AND x.deptype = 'e'
  )
UNION ALL
SELECT
    n.nspname AS schema,
    'funcao' AS classe,
    p.proname AS nome,
    p.prokind::text AS tipo,
    pg_get_function_identity_arguments(p.oid) AS argumentos,
    NULL::boolean AS tem_select,
    has_function_privilege(p.oid, 'EXECUTE') AS tem_execute,
    'ok' AS acesso
FROM pg_proc AS p
JOIN pg_namespace AS n ON n.oid = p.pronamespace
WHERE n.nspname = %(schema)s
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND n.nspname !~ '^pg_(toast_)?temp_'
  AND NOT EXISTS (
      SELECT 1 FROM pg_depend AS x
      WHERE x.classid = 'pg_proc'::regclass AND x.objid = p.oid AND x.deptype = 'e'
  )
ORDER BY schema, classe, nome, argumentos
