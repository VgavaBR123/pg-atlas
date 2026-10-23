-- Tipos definidos pelo usuário num schema: compostos (c), domínios (d) e enums (e).
-- Parâmetro: %(schema)s (texto), um schema por vez.
-- Tipos de extensões (pg_depend, deptype 'e') ficam de fora. Os valores de enum são
-- metadado do esquema, não dado de tabela. Tipos de linha de tabelas não entram.
-- Colunas: schema, nome, categoria (composto, dominio, enum), dono, comentario,
-- tipo_base (domínio), obrigatorio (domínio), padrao (domínio), restricoes (domínio),
-- valores (enum, na ordem de declaração), atributos (composto: "nome tipo", na ordem).
SELECT
    n.nspname AS schema,
    t.typname AS nome,
    CASE t.typtype
        WHEN 'c' THEN 'composto'
        WHEN 'd' THEN 'dominio'
        WHEN 'e' THEN 'enum'
    END AS categoria,
    pg_get_userbyid(t.typowner) AS dono,
    obj_description(t.oid, 'pg_type') AS comentario,
    CASE WHEN t.typtype = 'd' THEN format_type(t.typbasetype, t.typtypmod) END AS tipo_base,
    CASE WHEN t.typtype = 'd' THEN t.typnotnull END AS obrigatorio,
    CASE WHEN t.typtype = 'd' THEN t.typdefault END AS padrao,
    (
        SELECT array_agg(pg_get_constraintdef(k.oid) ORDER BY k.conname)
        FROM pg_constraint AS k
        WHERE k.contypid = t.oid
    ) AS restricoes,
    (
        SELECT array_agg(e.enumlabel ORDER BY e.enumsortorder)
        FROM pg_enum AS e
        WHERE e.enumtypid = t.oid
    ) AS valores,
    CASE WHEN t.typtype = 'c' THEN (
        SELECT array_agg(a.attname || ' ' || format_type(a.atttypid, a.atttypmod) ORDER BY a.attnum)
        FROM pg_attribute AS a
        WHERE a.attrelid = t.typrelid AND a.attnum > 0 AND NOT a.attisdropped
    ) END AS atributos
FROM pg_type AS t
JOIN pg_namespace AS n ON n.oid = t.typnamespace
WHERE n.nspname = %(schema)s
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND n.nspname !~ '^pg_(toast_)?temp_'
  AND (
      t.typtype IN ('d', 'e')
      OR (
          t.typtype = 'c'
          AND EXISTS (SELECT 1 FROM pg_class AS r WHERE r.oid = t.typrelid AND r.relkind = 'c')
      )
  )
  AND NOT EXISTS (
      SELECT 1 FROM pg_depend AS x
      WHERE x.classid = 'pg_type'::regclass AND x.objid = t.oid AND x.deptype = 'e'
  )
ORDER BY n.nspname, t.typname
