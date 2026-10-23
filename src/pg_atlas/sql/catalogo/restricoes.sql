-- Restrições de tabela de um schema: PK (p), UK (u), FK (f) e CHECK (c).
-- Parâmetro: %(schema)s (texto), um schema por vez (schema da tabela dona).
-- Relações de extensões (pg_depend, deptype 'e') ficam de fora.
-- Colunas: schema, relacao, nome, tipo, colunas (na ordem da restrição),
-- ref_schema, ref_relacao, ref_colunas (só FK), acao_delete e acao_update
-- (a = sem ação, r = restringe, c = cascata, n = anula, d = padrão), tipo_match,
-- adiavel, adiada, validada, herdada, definicao (pg_get_constraintdef).
SELECT
    n.nspname AS schema,
    c.relname AS relacao,
    k.conname AS nome,
    k.contype::text AS tipo,
    (
        SELECT array_agg(a.attname ORDER BY u.ord)
        FROM unnest(k.conkey) WITH ORDINALITY AS u(attnum, ord)
        JOIN pg_attribute AS a ON a.attrelid = k.conrelid AND a.attnum = u.attnum
    ) AS colunas,
    rn.nspname AS ref_schema,
    rc.relname AS ref_relacao,
    (
        SELECT array_agg(a.attname ORDER BY u.ord)
        FROM unnest(k.confkey) WITH ORDINALITY AS u(attnum, ord)
        JOIN pg_attribute AS a ON a.attrelid = k.confrelid AND a.attnum = u.attnum
    ) AS ref_colunas,
    CASE WHEN k.contype = 'f' THEN k.confdeltype::text END AS acao_delete,
    CASE WHEN k.contype = 'f' THEN k.confupdtype::text END AS acao_update,
    CASE WHEN k.contype = 'f' THEN k.confmatchtype::text END AS tipo_match,
    k.condeferrable AS adiavel,
    k.condeferred AS adiada,
    k.convalidated AS validada,
    k.conparentid <> 0 AS herdada,
    pg_get_constraintdef(k.oid) AS definicao
FROM pg_constraint AS k
JOIN pg_class AS c ON c.oid = k.conrelid
JOIN pg_namespace AS n ON n.oid = c.relnamespace
LEFT JOIN pg_class AS rc ON rc.oid = k.confrelid AND k.contype = 'f'
LEFT JOIN pg_namespace AS rn ON rn.oid = rc.relnamespace
WHERE n.nspname = %(schema)s
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND n.nspname !~ '^pg_(toast_)?temp_'
  AND k.contype IN ('p', 'u', 'f', 'c')
  AND NOT EXISTS (
      SELECT 1 FROM pg_depend AS x
      WHERE x.classid = 'pg_class'::regclass AND x.objid = c.oid AND x.deptype = 'e'
  )
ORDER BY n.nspname, c.relname, k.conname
