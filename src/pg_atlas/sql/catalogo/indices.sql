-- Índices das relações de um schema.
-- Parâmetro: %(schema)s (texto), um schema por vez (schema da tabela indexada).
-- Relações de extensões (pg_depend, deptype 'e') ficam de fora.
-- Colunas: schema, relacao, nome, metodo, colunas (colunas-chave ou expressões, na ordem),
-- unico, primario, parcial, predicado, valido, definicao (pg_get_indexdef).
SELECT
    n.nspname AS schema,
    t.relname AS relacao,
    ic.relname AS nome,
    am.amname AS metodo,
    (
        SELECT array_agg(pg_get_indexdef(i.indexrelid, g.ord::int, true) ORDER BY g.ord)
        FROM generate_series(1, i.indnkeyatts::int) AS g(ord)
    ) AS colunas,
    i.indisunique AS unico,
    i.indisprimary AS primario,
    i.indpred IS NOT NULL AS parcial,
    pg_get_expr(i.indpred, i.indrelid) AS predicado,
    i.indisvalid AS valido,
    pg_get_indexdef(i.indexrelid) AS definicao
FROM pg_index AS i
JOIN pg_class AS ic ON ic.oid = i.indexrelid
JOIN pg_class AS t ON t.oid = i.indrelid
JOIN pg_namespace AS n ON n.oid = t.relnamespace
JOIN pg_am AS am ON am.oid = ic.relam
WHERE n.nspname = %(schema)s
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND n.nspname !~ '^pg_(toast_)?temp_'
  AND NOT EXISTS (
      SELECT 1 FROM pg_depend AS x
      WHERE x.classid = 'pg_class'::regclass AND x.objid = t.oid AND x.deptype = 'e'
  )
ORDER BY n.nspname, t.relname, ic.relname
