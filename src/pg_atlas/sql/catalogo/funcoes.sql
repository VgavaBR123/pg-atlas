-- Funções, procedimentos, agregados e funções de janela de um schema.
-- Parâmetro: %(schema)s (texto), um schema por vez.
-- Escolha: rotinas que pertencem a extensões (pg_depend, deptype 'e') ficam de fora.
-- O código (pg_get_functiondef) não é pedido para agregados (prokind 'a'), pois falha neles.
-- Colunas: schema, nome, tipo (function, procedure, aggregate, window), linguagem,
-- argumentos, argumentos_identidade, retorno, volatilidade ('i' imutável, 's' estável,
-- 'v' volátil), security_definer, estrita, dono, comentario, codigo.
SELECT
    n.nspname AS schema,
    p.proname AS nome,
    CASE p.prokind
        WHEN 'f' THEN 'function'
        WHEN 'p' THEN 'procedure'
        WHEN 'a' THEN 'aggregate'
        WHEN 'w' THEN 'window'
    END AS tipo,
    l.lanname AS linguagem,
    pg_get_function_arguments(p.oid) AS argumentos,
    pg_get_function_identity_arguments(p.oid) AS argumentos_identidade,
    pg_get_function_result(p.oid) AS retorno,
    p.provolatile::text AS volatilidade,
    p.prosecdef AS security_definer,
    p.proisstrict AS estrita,
    pg_get_userbyid(p.proowner) AS dono,
    obj_description(p.oid, 'pg_proc') AS comentario,
    CASE WHEN p.prokind <> 'a' THEN pg_get_functiondef(p.oid) END AS codigo
FROM pg_proc AS p
JOIN pg_namespace AS n ON n.oid = p.pronamespace
JOIN pg_language AS l ON l.oid = p.prolang
WHERE n.nspname = %(schema)s
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND n.nspname !~ '^pg_(toast_)?temp_'
  AND NOT EXISTS (
      SELECT 1 FROM pg_depend AS x
      WHERE x.classid = 'pg_proc'::regclass AND x.objid = p.oid AND x.deptype = 'e'
  )
ORDER BY n.nspname, p.proname, pg_get_function_identity_arguments(p.oid)
