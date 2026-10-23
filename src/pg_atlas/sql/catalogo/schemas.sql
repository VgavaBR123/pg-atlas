-- Schemas do banco: nome, dono e comentário.
-- Parâmetro: %(schema)s (texto). NULL lista todos os schemas de usuário;
-- um nome restringe ao schema indicado.
-- Colunas: schema, dono, comentario.
SELECT
    n.nspname AS schema,
    pg_get_userbyid(n.nspowner) AS dono,
    obj_description(n.oid, 'pg_namespace') AS comentario
FROM pg_namespace AS n
WHERE (%(schema)s::text IS NULL OR n.nspname = %(schema)s::text)
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND n.nspname !~ '^pg_(toast_)?temp_'
ORDER BY n.nspname
