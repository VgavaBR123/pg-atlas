-- Relações de um schema: tabelas (r), particionadas (p), views (v),
-- materializadas (m) e estrangeiras (f).
-- Parâmetro: %(schema)s (texto), um schema por vez.
-- Escolha: relações que pertencem a extensões (pg_depend, deptype 'e') ficam de fora,
-- pois não fazem parte do modelo do banco documentado.
-- Colunas: schema, nome, tipo, dono, persistencia, comentario, eh_particao,
-- pai_schema, pai_nome, limite_particao, chave_particao.
SELECT
    n.nspname AS schema,
    c.relname AS nome,
    c.relkind::text AS tipo,
    pg_get_userbyid(c.relowner) AS dono,
    c.relpersistence::text AS persistencia,
    obj_description(c.oid, 'pg_class') AS comentario,
    c.relispartition AS eh_particao,
    pn.nspname AS pai_schema,
    pc.relname AS pai_nome,
    CASE WHEN c.relispartition THEN pg_get_expr(c.relpartbound, c.oid) END AS limite_particao,
    CASE WHEN c.relkind = 'p' THEN pg_get_partkeydef(c.oid) END AS chave_particao
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
LEFT JOIN pg_inherits AS i ON i.inhrelid = c.oid AND c.relispartition
LEFT JOIN pg_class AS pc ON pc.oid = i.inhparent
LEFT JOIN pg_namespace AS pn ON pn.oid = pc.relnamespace
WHERE n.nspname = %(schema)s
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND n.nspname !~ '^pg_(toast_)?temp_'
  AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND NOT EXISTS (
      SELECT 1 FROM pg_depend AS x
      WHERE x.classid = 'pg_class'::regclass AND x.objid = c.oid AND x.deptype = 'e'
  )
ORDER BY n.nspname, c.relname
