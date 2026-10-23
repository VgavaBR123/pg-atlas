-- Sequências de um schema (somente definição; o valor atual não é lido).
-- Parâmetro: %(schema)s (texto), um schema por vez.
-- Relações de extensões (pg_depend, deptype 'e') ficam de fora.
-- Colunas: schema, nome, tipo (tipo de dado), inicio, minimo, maximo, incremento, ciclica,
-- cache, dono (papel), coluna_schema, coluna_relacao, coluna_nome (coluna dona da
-- sequência, se houver), por_identidade (a sequência é de coluna de identidade).
SELECT
    n.nspname AS schema,
    c.relname AS nome,
    format_type(s.seqtypid, NULL) AS tipo,
    s.seqstart AS inicio,
    s.seqmin AS minimo,
    s.seqmax AS maximo,
    s.seqincrement AS incremento,
    s.seqcycle AS ciclica,
    s.seqcache AS cache,
    pg_get_userbyid(c.relowner) AS dono,
    tn.nspname AS coluna_schema,
    tc.relname AS coluna_relacao,
    a.attname AS coluna_nome,
    d.deptype = 'i' AS por_identidade
FROM pg_sequence AS s
JOIN pg_class AS c ON c.oid = s.seqrelid
JOIN pg_namespace AS n ON n.oid = c.relnamespace
LEFT JOIN pg_depend AS d
    ON d.classid = 'pg_class'::regclass
   AND d.objid = c.oid
   AND d.refclassid = 'pg_class'::regclass
   AND d.deptype IN ('a', 'i')
LEFT JOIN pg_class AS tc ON tc.oid = d.refobjid
LEFT JOIN pg_namespace AS tn ON tn.oid = tc.relnamespace
LEFT JOIN pg_attribute AS a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
WHERE n.nspname = %(schema)s
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND n.nspname !~ '^pg_(toast_)?temp_'
  AND NOT EXISTS (
      SELECT 1 FROM pg_depend AS x
      WHERE x.classid = 'pg_class'::regclass AND x.objid = c.oid AND x.deptype = 'e'
  )
ORDER BY n.nspname, c.relname, tn.nspname, tc.relname, a.attname
