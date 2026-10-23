-- Contagem de objetos por schema, para conferir o catálogo extraído (tarefa 1.5).
-- Sem parâmetros: devolve todos os schemas de usuário (a CLI aplica o escopo do projeto.yml).
-- Usa os mesmos filtros de sql/catalogo/: fora schemas de sistema e temporários, fora objetos
-- de extensões (pg_depend, deptype 'e'); relações com relkind r, p, v, m, f; sequências com
-- relkind S; rotinas de qualquer prokind. Só lê pg_namespace, pg_class, pg_proc e pg_depend.
-- Colunas: schema, tabela, particionada, view, materializada, estrangeira, sequencias, funcoes.
SELECT
    n.nspname AS schema,
    (SELECT count(*) FROM pg_class AS c
      WHERE c.relnamespace = n.oid AND c.relkind = 'r'
        AND NOT EXISTS (SELECT 1 FROM pg_depend AS x
                        WHERE x.classid = 'pg_class'::regclass AND x.objid = c.oid AND x.deptype = 'e')
    ) AS tabela,
    (SELECT count(*) FROM pg_class AS c
      WHERE c.relnamespace = n.oid AND c.relkind = 'p'
        AND NOT EXISTS (SELECT 1 FROM pg_depend AS x
                        WHERE x.classid = 'pg_class'::regclass AND x.objid = c.oid AND x.deptype = 'e')
    ) AS particionada,
    (SELECT count(*) FROM pg_class AS c
      WHERE c.relnamespace = n.oid AND c.relkind = 'v'
        AND NOT EXISTS (SELECT 1 FROM pg_depend AS x
                        WHERE x.classid = 'pg_class'::regclass AND x.objid = c.oid AND x.deptype = 'e')
    ) AS view,
    (SELECT count(*) FROM pg_class AS c
      WHERE c.relnamespace = n.oid AND c.relkind = 'm'
        AND NOT EXISTS (SELECT 1 FROM pg_depend AS x
                        WHERE x.classid = 'pg_class'::regclass AND x.objid = c.oid AND x.deptype = 'e')
    ) AS materializada,
    (SELECT count(*) FROM pg_class AS c
      WHERE c.relnamespace = n.oid AND c.relkind = 'f'
        AND NOT EXISTS (SELECT 1 FROM pg_depend AS x
                        WHERE x.classid = 'pg_class'::regclass AND x.objid = c.oid AND x.deptype = 'e')
    ) AS estrangeira,
    (SELECT count(*) FROM pg_class AS c
      WHERE c.relnamespace = n.oid AND c.relkind = 'S'
        AND NOT EXISTS (SELECT 1 FROM pg_depend AS x
                        WHERE x.classid = 'pg_class'::regclass AND x.objid = c.oid AND x.deptype = 'e')
    ) AS sequencias,
    (SELECT count(*) FROM pg_proc AS p
      WHERE p.pronamespace = n.oid
        AND NOT EXISTS (SELECT 1 FROM pg_depend AS x
                        WHERE x.classid = 'pg_proc'::regclass AND x.objid = p.oid AND x.deptype = 'e')
    ) AS funcoes
FROM pg_namespace AS n
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND n.nspname !~ '^pg_(toast_)?temp_'
ORDER BY n.nspname
