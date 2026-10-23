"""Testes offline da inferência por código (tarefa 2.3, D-018). Catálogo sintético e genérico."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pg_atlas import cli, codigo_extracao, relacionamentos_codigo

BIGINT = "bigint"


# ----------------------------------------------------------------- montagem


def tab(
    nome: str,
    colunas: dict[str, str] | None = None,
    pk: tuple[str, ...] | None = ("id",),
    unicas: tuple[tuple[str, ...], ...] = (),
    indices: tuple[dict, ...] = (),
    fks: tuple[dict, ...] = (),
    tipo: str = "tabela",
    definicao: str | None = None,
    particao: dict | None = None,
) -> dict:
    cols = dict(colunas or {})
    for c in pk or ():
        cols.setdefault(c, BIGINT)
    restricoes = list(fks)
    if pk:
        restricoes.append({"nome": f"pk_{nome}", "tipo": "pk", "colunas": list(pk)})
    for i, u in enumerate(unicas):
        restricoes.append({"nome": f"uk_{nome}_{i}", "tipo": "uk", "colunas": list(u)})
        for c in u:
            cols.setdefault(c, BIGINT)
    return {
        "nome": nome, "tipo": tipo, "particao": particao, "definicao": definicao,
        "colunas": [{"nome": c, "tipo": t, "ordem": i} for i, (c, t) in enumerate(cols.items())],
        "restricoes": restricoes, "indices": list(indices),
    }


def visao(nome: str, definicao: str, tipo: str = "view") -> dict:
    return tab(nome, {"x": "integer"}, pk=None, tipo=tipo, definicao=definicao)


def fk(nome: str, cols: list[str], ref_schema: str, ref_rel: str, ref_cols: list[str]) -> dict:
    return {
        "nome": nome, "tipo": "fk", "colunas": cols, "ref_schema": ref_schema, "ref_relacao": ref_rel,
        "ref_colunas": ref_cols, "validada": True, "acao_delete": None, "acao_update": None,
    }


def funcao(nome: str, corpo: str, linguagem: str = "plpgsql", retorno: str = "void", args: str = "") -> dict:
    codigo = (
        f"CREATE OR REPLACE FUNCTION a.{nome}({args})\n RETURNS {retorno}\n LANGUAGE {linguagem}\n"
        f"AS $function$\n{corpo}\n$function$\n"
    )
    return {
        "nome": nome, "tipo": "function", "linguagem": linguagem, "codigo": codigo,
        "argumentos_identidade": args,
    }


def plpgsql(*comandos: str) -> str:
    return "BEGIN\n" + "\n".join(f"  {c}" for c in comandos) + "\n  RETURN;\nEND"


def catalogo_base() -> list[dict]:
    """Tabelas do schema `a` usadas pela maioria dos testes (já com uma FK, exigida pelo grafo)."""
    return [
        tab("cliente", {"nome": "text", "id_pai": BIGINT}),
        tab("pedido", {"id_cli": BIGINT, "id_cli_alt": BIGINT, "valor": "numeric"},
            fks=(fk("fk_ped_cli", ["id_cli_alt"], "a", "cliente", ["id"]),)),
        tab("item", {"id_pedido": BIGINT}),
    ]


def montar(tmp_path: Path, schemas: dict[str, list[dict]], fatos: str | None = None) -> Path:
    doc = tmp_path / "doc"
    (doc / "config").mkdir(parents=True)
    (doc / "config" / "projeto.yml").write_text('projeto:\n  nome: "t"\n', encoding="utf-8")
    if fatos is not None:
        (doc / "config" / "fatos.yml").write_text(fatos, encoding="utf-8")
    pasta = doc / "catalogo" / "estrutura"
    pasta.mkdir(parents=True)
    for schema, relacoes in schemas.items():
        funcoes = [r for r in relacoes if "linguagem" in r]
        rels = [r for r in relacoes if "linguagem" not in r]
        (pasta / f"{schema}.json").write_text(
            json.dumps({"formato": "estrutura", "schema": schema, "relacoes": rels, "funcoes": funcoes}),
            encoding="utf-8",
        )
    return doc


def com(extra: list[dict], extra_b: list[dict] | None = None) -> dict[str, list[dict]]:
    return {"a": catalogo_base() + extra, "b": extra_b or []}


def caminho_grafo(doc: Path) -> Path:
    return doc / "relacionamentos" / "grafo.json"


def caminho_falhas(doc: Path) -> Path:
    return doc / "_controle" / "codigo-nao-analisado.json"


def rodar(doc: Path, *extra: str, etapa: str = "codigo") -> dict[str, Any]:
    if not caminho_grafo(doc).exists():
        assert cli.main(["relacionamentos", "fks", "--doc", str(doc)]) == 0
    assert cli.main(["relacionamentos", etapa, "--doc", str(doc), *extra]) == 0
    return json.loads(caminho_grafo(doc).read_text(encoding="utf-8"))


def codigo(grafo: dict) -> dict[str, dict]:
    return {a["id"]: a for a in grafo["arestas"] if a["fonte"] == "inferencia_codigo"}


def bloco(grafo: dict) -> dict[str, Any]:
    return grafo["resumo"]["inferencia_codigo"]


def so_view(tmp_path: Path, definicao: str, extra: list[dict] | None = None, fatos: str | None = None) -> dict:
    doc = montar(tmp_path, com([visao("v1", definicao)] + (extra or [])), fatos)
    return rodar(doc)


JOIN_BASICO = "SELECT 1 FROM a.pedido p JOIN a.cliente c ON c.id = p.id_cli"
ID_BASICO = "codigo:a.pedido.id_cli->a.cliente.id"


# ----------------------------------------------------------------- views e formato


def test_join_de_view_gera_aresta_com_evidencia_sem_codigo(tmp_path: Path) -> None:
    g = so_view(tmp_path, JOIN_BASICO)
    a = codigo(g)[ID_BASICO]
    assert a["confianca"] == "hipotese" and a["taxa_casamento"] is None and a["nota"] is None
    assert a["origem"] == {"schema": "a", "relacao": "pedido", "colunas": ["id_cli"]}
    assert a["destino"] == {"schema": "a", "relacao": "cliente", "colunas": ["id"]}
    assert a["evidencia"] == [{
        "tipo": "inferencia_codigo", "objetos": [{"schema": "a", "nome": "v1", "tipo": "view"}],
        "total_objetos": 1, "tipos_iguais": True, "chave_sem_tenant": False,
    }]
    assert "SELECT" not in json.dumps(g)  # nenhum trecho de código no grafo
    b = bloco(g)
    assert b["arestas"] == 1 and b["objetos_analisados"] == {"view": 1} and b["comandos_analisados"] == 1
    assert b["igualdades"] == 1 and b["extrator"] in ("pglast", "heuristico")


def test_materializada_e_where_com_virgula_e_alias_com_aspas(tmp_path: Path) -> None:
    doc = montar(tmp_path, com([
        visao("m1", 'SELECT 1 FROM a.pedido "P", a.cliente c WHERE (c.id = "P".id_cli AND 1 = 1)', "materializada"),
    ]))
    g = rodar(doc)
    assert list(codigo(g)) == [ID_BASICO]
    assert codigo(g)[ID_BASICO]["evidencia"][0]["objetos"][0]["tipo"] == "materializada"
    assert bloco(g)["objetos_analisados"] == {"materializada": 1}


def test_cast_dos_dois_lados_e_tipos_diferentes(tmp_path: Path) -> None:
    doc = montar(tmp_path, com([
        tab("texto", {"ref": "character varying(10)"}),
        visao("v1", "SELECT 1 FROM a.texto t JOIN a.cliente c ON (t.ref)::bigint = c.id"),
    ]))
    a = codigo(rodar(doc))["codigo:a.texto.ref->a.cliente.id"]
    assert a["evidencia"][0]["tipos_iguais"] is False


def test_subconsulta_correlacionada_enxerga_alias_externo(tmp_path: Path) -> None:
    g = so_view(tmp_path, "SELECT 1 FROM a.cliente c WHERE EXISTS (SELECT 1 FROM a.pedido p WHERE p.id_cli = c.id)")
    assert list(codigo(g)) == [ID_BASICO]


def test_cte_subconsulta_e_union_sao_escopos(tmp_path: Path) -> None:
    g = so_view(
        tmp_path,
        "WITH x AS (SELECT p.id_cli FROM a.pedido p JOIN a.cliente c ON c.id = p.id_cli) "
        "SELECT 1 FROM x UNION SELECT 2 FROM a.item i JOIN a.pedido p2 ON p2.id = i.id_pedido",
    )
    assert sorted(codigo(g)) == sorted([ID_BASICO, "codigo:a.item.id_pedido->a.pedido.id"])


def test_termo_dentro_de_or_fica_de_fora(tmp_path: Path) -> None:
    g = so_view(tmp_path, "SELECT 1 FROM a.pedido p, a.cliente c WHERE p.id_cli = c.id OR p.valor = c.id")
    assert codigo(g) == {} and bloco(g)["igualdades"] == 0


def test_coluna_nao_qualificada_e_coluna_inexistente(tmp_path: Path) -> None:
    g = so_view(
        tmp_path,
        "SELECT 1 FROM a.pedido p JOIN a.cliente c ON id_cli = c.id AND p.nao_existe = c.id",
    )
    b = bloco(g)
    assert codigo(g) == {}
    assert b["coluna_nao_qualificada"] == 1 and b["coluna_inexistente"] == 1 and b["igualdades"] == 2


def test_autojuncao_trivial_descartada_e_autorreferencia_permitida(tmp_path: Path) -> None:
    g = so_view(tmp_path, "SELECT 1 FROM a.cliente x JOIN a.cliente y ON x.id = y.id JOIN a.cliente z ON z.id = x.id_pai")
    assert list(codigo(g)) == ["codigo:a.cliente.id_pai->a.cliente.id"]
    assert bloco(g)["autojuncao_trivial"] == 1


def test_lados_view_cte_e_nao_resolvido(tmp_path: Path) -> None:
    doc = montar(tmp_path, com([
        visao("v0", "SELECT 1 FROM a.item"),
        visao("v1", "SELECT 1 FROM a.pedido p JOIN a.v0 w ON w.x = p.id"),
        visao("v2", "WITH c AS (SELECT 1 AS k) SELECT 1 FROM a.pedido p JOIN c ON c.k = p.id"),
        visao("v3", "SELECT 1 FROM a.pedido p JOIN a.fantasma f ON f.id = p.id"),
        visao("v4", "SELECT 1 FROM a.pedido p JOIN (SELECT 1 AS k) d ON d.k = p.id"),
        visao("v5", "SELECT 1 FROM a.pedido p WHERE rec.campo = p.id"),
    ]))
    b = bloco(rodar(doc))
    assert b["lado_view"] == 1 and b["lado_nao_relacao"] == 2 and b["lado_nao_resolvido"] == 2
    assert b["arestas"] == 0


def test_nome_nao_qualificado_usa_schema_do_objeto_e_depois_nome_unico(tmp_path: Path) -> None:
    doc = montar(
        tmp_path,
        com([visao("v1", "SELECT 1 FROM pedido p JOIN cliente c ON c.id = p.id_cli JOIN unico u ON u.id = c.id_pai")],
            [tab("unico")]),
    )
    g = rodar(doc)
    assert sorted(codigo(g)) == sorted([ID_BASICO, "codigo:a.cliente.id_pai->b.unico.id"])


def test_particao_vira_tabela_particionada(tmp_path: Path) -> None:
    doc = montar(tmp_path, com([
        tab("pedido_p1", {"id_cli": BIGINT}, particao={"pai_schema": "a", "pai_nome": "pedido", "limite": "x"}),
        visao("v1", "SELECT 1 FROM a.pedido_p1 p JOIN a.cliente c ON c.id = p.id_cli"),
    ]))
    assert list(codigo(rodar(doc))) == [ID_BASICO]


def test_using_usa_as_duas_relacoes(tmp_path: Path) -> None:
    doc = montar(tmp_path, com([
        tab("endereco", {"codigo": "text"}),
        tab("regiao", {"codigo": "text"}, indices=({"nome": "ix", "colunas": ["codigo"], "unico": True, "parcial": False},)),
        visao("v1", "SELECT 1 FROM a.endereco e JOIN a.regiao r USING (codigo)"),
    ]))
    assert list(codigo(rodar(doc))) == ["codigo:a.endereco.codigo->a.regiao.codigo"]


def test_indice_unico_parcial_ou_com_expressao_nao_e_chave(tmp_path: Path) -> None:
    doc = montar(tmp_path, com([
        tab("endereco", {"codigo": "text"}),
        tab("regiao", {"codigo": "text"}, indices=(
            {"nome": "ix1", "colunas": ["codigo"], "unico": True, "parcial": True},
            {"nome": "ix2", "colunas": ["lower(codigo)"], "unico": True, "parcial": False},
        )),
        visao("v1", "SELECT 1 FROM a.endereco e JOIN a.regiao r USING (codigo)"),
    ]))
    g = rodar(doc)
    assert codigo(g) == {} and bloco(g)["sem_chave"] == 1


# ----------------------------------------------------------------- direção, 1:1, repetição


def test_chave_unica_contra_pk_o_destino_e_a_pk(tmp_path: Path) -> None:
    doc = montar(tmp_path, com([
        tab("perfil", {"id_cli": BIGINT}, unicas=(("id_cli",),)),
        visao("v1", "SELECT 1 FROM a.perfil f JOIN a.cliente c ON c.id = f.id_cli"),
    ]))
    g = rodar(doc)
    a = codigo(g)["codigo:a.perfil.id_cli->a.cliente.id"]
    assert a["nota"] is None and bloco(g)["um_para_um"] == 0


def test_dois_lados_pk_geram_uma_aresta_1_para_1(tmp_path: Path) -> None:
    doc = montar(tmp_path, com([
        tab("t1"), tab("t2"),
        visao("v1", "SELECT 1 FROM a.t2 y JOIN a.t1 x ON y.id = x.id"),
    ]))
    g = rodar(doc)
    assert list(codigo(g)) == ["codigo:a.t1.id->a.t2.id"]
    assert codigo(g)["codigo:a.t1.id->a.t2.id"]["nota"] == "1:1: direção não determinada pelo código"
    assert bloco(g)["um_para_um"] == 1


def test_dois_lados_so_unique_tambem_sao_1_para_1(tmp_path: Path) -> None:
    doc = montar(tmp_path, com([
        tab("t1", {"k": "text"}, unicas=(("k",),)), tab("t2", {"k": "text"}, unicas=(("k",),)),
        visao("v1", "SELECT 1 FROM a.t2 y JOIN a.t1 x ON y.k = x.k"),
    ]))
    g = rodar(doc)
    assert codigo(g)["codigo:a.t1.k->a.t2.k"]["nota"].startswith("1:1")


def test_colisao_de_id_evitada_pelas_colunas_de_destino(tmp_path: Path) -> None:
    doc = montar(tmp_path, com([
        tab("alvo", {"k1": BIGINT, "k2": BIGINT}, unicas=(("k1",), ("k2",))),
        tab("fonte", {"ref": BIGINT}),
        visao("v1", "SELECT 1 FROM a.fonte f JOIN a.alvo t ON t.k1 = f.ref"),
        visao("v2", "SELECT 1 FROM a.fonte f JOIN a.alvo t ON t.k2 = f.ref"),
    ]))
    assert sorted(codigo(rodar(doc))) == ["codigo:a.fonte.ref->a.alvo.k1", "codigo:a.fonte.ref->a.alvo.k2"]


def test_varias_colunas_em_pares_ordenados_pela_origem(tmp_path: Path) -> None:
    doc = montar(tmp_path, com([
        tab("comp", {"y": BIGINT, "x": BIGINT}, pk=("x", "y")),
        tab("uso", {"ay": BIGINT, "ax": BIGINT}),
        visao("v1", "SELECT 1 FROM a.uso u JOIN a.comp c ON c.y = u.ay AND c.x = u.ax"),
    ]))
    a = codigo(rodar(doc))["codigo:a.uso.ax,ay->a.comp.x,y"]
    assert a["origem"]["colunas"] == ["ax", "ay"] and a["destino"]["colunas"] == ["x", "y"]


def test_aresta_agregada_por_varios_objetos_sem_repeticao(tmp_path: Path) -> None:
    doc = montar(tmp_path, com([
        visao("v2", JOIN_BASICO), visao("v1", JOIN_BASICO),
        funcao("f", plpgsql(f"PERFORM 1 FROM a.pedido p JOIN a.cliente c ON c.id = p.id_cli;")),
        funcao("f", plpgsql(f"PERFORM 1 FROM a.pedido p JOIN a.cliente c ON c.id = p.id_cli;"), args="integer"),
    ]))
    g = rodar(doc)
    ev = codigo(g)[ID_BASICO]["evidencia"][0]
    assert ev["total_objetos"] == 3
    assert ev["objetos"] == [
        {"schema": "a", "nome": "f", "tipo": "function"},
        {"schema": "a", "nome": "v1", "tipo": "view"},
        {"schema": "a", "nome": "v2", "tipo": "view"},
    ]
    assert bloco(g)["objetos_analisados"] == {"function": 1, "view": 2}


def test_teto_de_vinte_objetos_na_evidencia(tmp_path: Path) -> None:
    doc = montar(tmp_path, com([visao(f"v{i:02d}", JOIN_BASICO) for i in range(25)]))
    ev = codigo(rodar(doc))[ID_BASICO]["evidencia"][0]
    assert ev["total_objetos"] == 25 and len(ev["objetos"]) == 20


def test_repete_fk_nao_gera_aresta(tmp_path: Path) -> None:
    g = so_view(tmp_path, "SELECT 1 FROM a.pedido p JOIN a.cliente c ON c.id = p.id_cli_alt")
    assert codigo(g) == {} and bloco(g)["repetem_fk"] == 1 and bloco(g)["arestas"] == 0
    fks = [a for a in g["arestas"] if a["fonte"] == "fk_declarada"]
    assert len(fks) == 1 and len(fks[0]["evidencia"]) == 1  # a evidência da FK não muda


def amb(extra: list[dict], extra_b: list[dict]) -> dict[str, list[dict]]:
    return com(extra, extra_b)


def test_repete_nome_e_desempata_nome(tmp_path: Path) -> None:
    doc = montar(tmp_path, amb(
        [tab("grupo"), tab("t", {"id_grupo": BIGINT}),
         visao("v1", "SELECT 1 FROM a.t t JOIN b.grupo g ON g.id = t.id_grupo")],
        [tab("grupo")],
    ))
    assert cli.main(["relacionamentos", "fks", "--doc", str(doc)]) == 0
    g = rodar(doc, etapa="nome")
    nome = [a for a in g["arestas"] if a["fonte"] == "inferencia_nome" and a["origem"]["colunas"] == ["id_grupo"]]
    assert len(nome) == 2 and all(a["nota"] for a in nome)  # ambíguo
    g = rodar(doc)
    assert list(codigo(g)) == ["codigo:a.t.id_grupo->b.grupo.id"]  # a aresta própria é gerada
    b = bloco(g)
    assert b["repetem_nome"] == 1 and b["desempata_nome"] == 1
    assert [a for a in g["arestas"] if a["fonte"] == "inferencia_nome" and a["origem"]["colunas"] == ["id_grupo"]] == nome


# ----------------------------------------------------------------- tenant

FATOS = 'tenant:\n  coluna: id_tenant\n  valores:\n    x: "1"\n'
TENANT = [
    tab("conta", {"id_tenant": BIGINT}, pk=("id_tenant", "id")),
    tab("lancto", {"id_tenant": BIGINT, "id_conta": BIGINT}),
    tab("param", {"id_tenant": BIGINT}),
]


def test_tenant_ignorado_e_chave_sem_tenant(tmp_path: Path) -> None:
    g = so_view(
        tmp_path,
        "SELECT 1 FROM a.lancto l JOIN a.conta c ON l.id_tenant = c.id_tenant AND l.id_conta = c.id",
        TENANT, FATOS,
    )
    a = codigo(g)["codigo:a.lancto.id_conta->a.conta.id"]
    assert a["evidencia"][0]["chave_sem_tenant"] is True
    assert a["origem"]["colunas"] == ["id_conta"] and bloco(g)["tenant_ignorada"] == 1


def test_chave_exata_sem_tenant_na_chave_nao_marca(tmp_path: Path) -> None:
    g = so_view(
        tmp_path,
        "SELECT 1 FROM a.lancto l JOIN a.param p ON l.id_tenant = p.id_tenant AND l.id_conta = p.id",
        TENANT, FATOS,
    )
    assert codigo(g)["codigo:a.lancto.id_conta->a.param.id"]["evidencia"][0]["chave_sem_tenant"] is False


def test_so_igualdade_de_tenant_nao_gera_aresta(tmp_path: Path) -> None:
    g = so_view(tmp_path, "SELECT 1 FROM a.lancto l JOIN a.param p ON l.id_tenant = p.id_tenant", TENANT, FATOS)
    assert codigo(g) == {} and bloco(g)["tenant_ignorada"] == 1 and bloco(g)["sem_chave"] == 0


def test_sem_tenant_declarado_a_chave_composta_e_comparada_inteira(tmp_path: Path) -> None:
    g = so_view(
        tmp_path,
        "SELECT 1 FROM a.lancto l JOIN a.conta c ON l.id_tenant = c.id_tenant AND l.id_conta = c.id",
        TENANT,
    )
    a = codigo(g)["codigo:a.lancto.id_conta,id_tenant->a.conta.id,id_tenant"]
    assert a["evidencia"][0]["chave_sem_tenant"] is False and bloco(g)["tenant_ignorada"] == 0


# ----------------------------------------------------------------- rotinas e extratores


CORPO_PLPGSQL = plpgsql(
    "PERFORM 1 FROM a.pedido p JOIN a.cliente c ON c.id = p.id_cli;",
    "EXECUTE 'select 1';",
)


@pytest.mark.parametrize("extrator", ["pglast", "heuristico"])
def test_rotina_plpgsql_pelos_dois_extratores(tmp_path: Path, extrator: str) -> None:
    if extrator == "pglast":
        pytest.importorskip("pglast")
    doc = montar(tmp_path, com([funcao("f", CORPO_PLPGSQL)]))
    extra = ["--extrator", "heuristico"] if extrator == "heuristico" else []
    g = rodar(doc, *extra)
    b = bloco(g)
    assert b["extrator"] == extrator
    assert list(codigo(g)) == [ID_BASICO] and b["execute_dinamico"] == 1
    assert b["objetos_analisados"] == {"function": 1}


def test_pglast_cobre_for_open_return_query_atribuicao_e_condicao(tmp_path: Path) -> None:
    pytest.importorskip("pglast")
    corpo = (
        "DECLARE r record; rc refcursor; v bigint;\n"
        "BEGIN\n"
        "  SELECT p.id INTO v FROM a.pedido p JOIN a.cliente c ON c.id = p.id_cli;\n"
        "  FOR r IN SELECT 1 FROM a.item i JOIN a.pedido p ON p.id = i.id_pedido LOOP v := 1; END LOOP;\n"
        "  OPEN rc FOR SELECT 1 FROM a.cliente c JOIN a.cliente d ON d.id = c.id_pai;\n"
        "  v := (SELECT max(1) FROM a.t1 x JOIN a.t2 y ON y.id = x.k);\n"
        "  IF EXISTS (SELECT 1 FROM a.t3 z JOIN a.cliente w ON w.id = z.k) THEN v := 2; END IF;\n"
        "  RETURN;\nEND"
    )
    doc = montar(tmp_path, com([
        tab("t1", {"k": BIGINT}), tab("t2"), tab("t3", {"k": BIGINT}), funcao("f", corpo),
    ]))
    g = rodar(doc)
    assert sorted(codigo(g)) == sorted([
        ID_BASICO, "codigo:a.item.id_pedido->a.pedido.id", "codigo:a.cliente.id_pai->a.cliente.id",
        "codigo:a.t1.k->a.t2.id", "codigo:a.t3.k->a.cliente.id",
    ])
    assert bloco(g)["extrator"] == "pglast" and bloco(g)["fallback_extracao"] == {}


def test_heuristico_cobre_rotulo_if_for_perform_into_e_execute(tmp_path: Path) -> None:
    corpo = (
        "DECLARE v bigint;\nBEGIN\n"
        "  -- comentário com ; e SELECT 1 FROM a.item\n"
        "  IF v > 1 THEN SELECT p.id INTO STRICT v FROM a.pedido p JOIN a.cliente c ON c.id = p.id_cli; END IF;\n"
        "  FOR r IN SELECT 1 FROM a.item i JOIN a.pedido p ON p.id = i.id_pedido LOOP NULL; END LOOP;\n"
        "  select into v max(1) from a.cliente c join a.cliente d on d.id = c.id_pai;\n"
        "  RETURN QUERY EXECUTE 'select 2';\n"
        "  EXECUTE 'select 3';\n  RETURN;\nEND"
    )
    doc = montar(tmp_path, com([funcao("f", corpo)]))
    g = rodar(doc, "--extrator", "heuristico")
    assert sorted(codigo(g)) == sorted([
        ID_BASICO, "codigo:a.item.id_pedido->a.pedido.id", "codigo:a.cliente.id_pai->a.cliente.id",
    ])
    assert bloco(g)["execute_dinamico"] == 2


def test_fallback_do_pglast_conta_o_motivo(tmp_path: Path) -> None:
    pytest.importorskip("pglast")
    corpo = plpgsql("PERFORM 1 FROM a.pedido p JOIN a.cliente c ON c.id = p.id_cli;")
    f = funcao("f", corpo, args="x a.tipo_qualquer")
    doc = montar(tmp_path, com([f]))
    g = rodar(doc)
    assert list(codigo(g)) == [ID_BASICO]
    assert bloco(g)["fallback_extracao"] == {"tipo_qualificado": 1}


def test_rotina_sql_divide_comandos_fora_de_strings(tmp_path: Path) -> None:
    corpo = (
        "SELECT ';' FROM a.item;\n"
        "SELECT 1 FROM a.pedido p JOIN a.cliente c ON c.id = p.id_cli /* ; */ WHERE p.valor > 0;\n"
        "SELECT $q$ ; $q$ AS x"
    )
    doc = montar(tmp_path, com([funcao("f", corpo, linguagem="sql", retorno="integer")]))
    g = rodar(doc)
    assert list(codigo(g)) == [ID_BASICO] and bloco(g)["comandos_analisados"] == 3


def test_objetos_fora_internal_sem_codigo_e_view_sem_definicao(tmp_path: Path) -> None:
    doc = montar(tmp_path, com([
        {"nome": "agg", "tipo": "aggregate", "linguagem": "internal", "codigo": None, "argumentos_identidade": ""},
        {"nome": "vazia", "tipo": "function", "linguagem": "plpgsql", "codigo": None, "argumentos_identidade": ""},
        tab("sem_def", pk=None, tipo="view", definicao=None),
    ]))
    b = bloco(rodar(doc))
    assert b["objetos_fora"] == {"aggregate": 1, "function": 1, "view": 1} and b["objetos_analisados"] == {}


# ----------------------------------------------------------------- falhas e limites


def test_parse_mascarado_e_comando_grande_vao_para_o_arquivo_de_falhas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(relacionamentos_codigo, "TAMANHO_MAXIMO_COMANDO", 120)
    doc = montar(tmp_path, com([
        visao("v_ok", JOIN_BASICO),
        visao("v_parse", "SELECT ( FROM a.cliente"),
        visao("v_masc", "SELECT ( FROM a.cliente WHERE x = [mascarado:cnpj:14]"),
        visao("v_grande", "SELECT " + ", ".join(f"c.coluna_{i}" for i in range(40)) + " FROM a.cliente c"),
        funcao("f", plpgsql(JOIN_BASICO + ";", "SELECT ( FROM a.cliente;")),
    ]))
    g = rodar(doc, "--extrator", "heuristico")
    assert bloco(g)["comandos_falhos"] == {"comando_grande": 1, "mascarado": 1, "parse": 2}
    itens = json.loads(caminho_falhas(doc).read_text(encoding="utf-8"))["itens"]
    assert [(i["nome"], i["tipo"], i["motivo"], i["comandos_falhos"], i["comandos_total"]) for i in itens] == [
        ("f", "function", "parse", 1, 2),
        ("v_grande", "view", "comando_grande", 1, 1),
        ("v_masc", "view", "mascarado", 1, 1),
        ("v_parse", "view", "parse", 1, 1),
    ]
    assert set(itens[0]) == {"schema", "nome", "tipo", "motivo", "comandos_falhos", "comandos_total"}
    assert "SELECT" not in caminho_falhas(doc).read_text(encoding="utf-8")


def test_excecao_na_analise_e_capturada_e_contada(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_: Any, **__: Any) -> None:
        raise RecursionError

    doc = montar(tmp_path, com([visao("v1", JOIN_BASICO)]))
    monkeypatch.setattr(relacionamentos_codigo.AnalisadorDeJoins, "analisar_arvores", explode)
    g = rodar(doc)
    assert bloco(g)["comandos_falhos"] == {"analise": 1} and codigo(g) == {}


# ----------------------------------------------------------------- extração (unidades)


def test_dividir_comandos_respeita_strings_aspas_dollar_e_comentarios() -> None:
    texto = "a; 'x;y' ; \"i;d\"; $t$ ; $t$; -- c;\nb /* ; /* aninhado ; */ ; */ c;"
    assert codigo_extracao.dividir_comandos(texto) == ["a", "'x;y'", '"i;d"', "$t$ ; $t$", "b   c"]


def test_corpo_da_rotina_e_remover_into() -> None:
    assert codigo_extracao.corpo_da_rotina("CREATE FUNCTION f() AS $x$ corpo $x$ SET a=1") == " corpo "
    assert codigo_extracao.corpo_da_rotina("sem delimitador") is None
    assert codigo_extracao.remover_into("select a into v1, v2 from t").split() == ["select", "a", "from", "t"]
    assert codigo_extracao.remover_into("select into v max(a) from t").split() == ["select", "max(a)", "from", "t"]
    assert codigo_extracao.remover_into("INSERT INTO t select 1") == "INSERT INTO t select 1"
    assert codigo_extracao.remover_into("with x as (select 1) insert into t select * from x").count("into") == 1


def test_heuristico_diretamente() -> None:
    corpo = "BEGIN <<lbl>> FOR r IN SELECT 1 FROM t LOOP NULL; END LOOP; ELSE PERFORM 1 FROM u; RETURN QUERY SELECT 2; END"
    e = codigo_extracao.extrair_heuristico(corpo, "plpgsql")
    assert e.comandos == ["SELECT 1 FROM t", "SELECT 1 FROM u", "SELECT 2"] and e.execute_dinamico == 0


# ----------------------------------------------------------------- preservação e determinismo


def test_preserva_outras_fontes_e_blocos_nos_dois_sentidos(tmp_path: Path) -> None:
    doc = montar(tmp_path, amb(
        [tab("grupo"), tab("t", {"id_grupo": BIGINT}), visao("v1", JOIN_BASICO)], [],
    ))
    assert cli.main(["relacionamentos", "fks", "--doc", str(doc)]) == 0
    g_nome = rodar(doc, etapa="nome")
    g = rodar(doc)
    assert [a for a in g["arestas"] if a["fonte"] == "inferencia_nome"] == [
        a for a in g_nome["arestas"] if a["fonte"] == "inferencia_nome"]
    assert g["resumo"]["inferencia_nome"] == g_nome["resumo"]["inferencia_nome"]
    n_nome = sum(a["fonte"] == "inferencia_nome" for a in g_nome["arestas"])
    assert n_nome >= 1
    assert g["resumo"]["por_fonte"] == {"fk_declarada": 1, "inferencia_codigo": 1, "inferencia_nome": n_nome}
    # nome e fks reexecutados preservam as arestas e o bloco do código
    for etapa in ("nome", "fks"):
        depois = rodar(doc, etapa=etapa)
        assert codigo(depois) == codigo(g) and bloco(depois) == bloco(g)
        assert depois["resumo"]["inferencia_nome"] == g["resumo"]["inferencia_nome"]
    # o código preserva uma aresta de fonte desconhecida
    sintetica = codigo(g)[ID_BASICO] | {"id": "amostra:x", "fonte": "amostragem"}
    g["arestas"].append(sintetica)
    caminho_grafo(doc).write_text(json.dumps(g), encoding="utf-8")
    assert "amostra:x" in {a["id"] for a in rodar(doc)["arestas"]}


def test_reexecucao_apaga_so_arestas_proprias_obsoletas(tmp_path: Path) -> None:
    doc = montar(tmp_path, com([visao("v1", JOIN_BASICO)]))
    assert list(codigo(rodar(doc))) == [ID_BASICO]
    catalogo = doc / "catalogo" / "estrutura" / "a.json"
    dados = json.loads(catalogo.read_text(encoding="utf-8"))
    for r in dados["relacoes"]:
        if r["nome"] == "v1":
            r["definicao"] = "SELECT 1 FROM a.item"
    catalogo.write_text(json.dumps(dados), encoding="utf-8")
    g = rodar(doc)
    assert codigo(g) == {} and len([a for a in g["arestas"] if a["fonte"] == "fk_declarada"]) == 1


def test_determinismo_bytes_iguais_inclusive_arquivo_de_falhas(tmp_path: Path) -> None:
    doc = montar(tmp_path, com([
        visao("v2", JOIN_BASICO), visao("v1", "SELECT ( FROM a.cliente"),
        funcao("f", CORPO_PLPGSQL), funcao("g", CORPO_PLPGSQL, args="integer"),
    ]))
    rodar(doc)
    antes = (caminho_grafo(doc).read_bytes(), caminho_falhas(doc).read_bytes())
    rodar(doc)
    assert (caminho_grafo(doc).read_bytes(), caminho_falhas(doc).read_bytes()) == antes
    assert antes[0].endswith(b"\n") and b"\r" not in antes[0] and b"\r" not in antes[1]


def test_falha_sem_catalogo_sem_grafo_sem_fks_e_grafo_invalido(tmp_path: Path) -> None:
    vazio = tmp_path / "vazio"
    (vazio / "config").mkdir(parents=True)
    (vazio / "config" / "projeto.yml").write_text('projeto:\n  nome: "t"\n', encoding="utf-8")
    assert cli.main(["relacionamentos", "codigo", "--doc", str(vazio)]) == 1
    doc = montar(tmp_path, com([visao("v1", JOIN_BASICO)]))
    assert cli.main(["relacionamentos", "codigo", "--doc", str(doc)]) == 1
    assert not caminho_grafo(doc).exists() and not caminho_falhas(doc).exists()
    caminho_grafo(doc).parent.mkdir()
    caminho_grafo(doc).write_text('{"versao_formato": 1, "arestas": "x"}', encoding="utf-8")
    assert cli.main(["relacionamentos", "codigo", "--doc", str(doc)]) == 1
    assert caminho_grafo(doc).read_text(encoding="utf-8") == '{"versao_formato": 1, "arestas": "x"}'
    caminho_grafo(doc).write_text(
        '{"versao_formato": 1, "arestas": [], "resumo": {"total": 0, "por_confianca": {}, "por_fonte": {}, '
        '"fk_declarada": {"autorreferencias": 0, "entre_schemas": 0, "not_valid": 0, "varias_colunas": 0}}}',
        encoding="utf-8",
    )
    assert cli.main(["relacionamentos", "codigo", "--doc", str(doc)]) == 1


def test_falha_id_repetido_sem_gravar(tmp_path: Path) -> None:
    doc = montar(tmp_path, com([visao("v1", JOIN_BASICO)]))
    g = rodar(doc)
    g["arestas"].append(codigo(g)[ID_BASICO] | {"fonte": "amostragem"})  # mesmo id, outra fonte
    caminho_grafo(doc).write_text(json.dumps(g), encoding="utf-8")
    antes = caminho_grafo(doc).read_bytes()
    assert cli.main(["relacionamentos", "codigo", "--doc", str(doc)]) == 1
    assert caminho_grafo(doc).read_bytes() == antes


def test_grafo_valido_pelo_esquema_com_evidencia_do_codigo(tmp_path: Path) -> None:
    from pg_atlas import relacionamentos

    g = so_view(tmp_path, JOIN_BASICO)
    relacionamentos.validar(g)
    codigo(g)[ID_BASICO]["evidencia"][0].pop("tipos_iguais")
    with pytest.raises(relacionamentos.ErroRelacionamentos):
        relacionamentos.validar(g)


def test_sem_pglast_instalado_cai_no_heuristico(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(relacionamentos_codigo, "pglast_disponivel", lambda: False)
    doc = montar(tmp_path, com([funcao("f", CORPO_PLPGSQL)]))
    g = rodar(doc)
    assert bloco(g)["extrator"] == "heuristico" and list(codigo(g)) == [ID_BASICO]
