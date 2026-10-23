"""Testes offline da inferência por nome (tarefa 2.2, D-017). Catálogo sintético e genérico."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pg_atlas import cli, relacionamentos, relacionamentos_nome
from pg_atlas.erros import ErroAtlas

BIGINT = "bigint"


def col(nome: str, tipo: str = BIGINT) -> dict:
    return {"nome": nome, "tipo": tipo, "ordem": 1}


def fk(nome: str, cols: list[str], ref_schema: str, ref_rel: str, ref_cols: list[str]) -> dict:
    return {
        "nome": nome, "tipo": "fk", "colunas": cols, "ref_schema": ref_schema, "ref_relacao": ref_rel,
        "ref_colunas": ref_cols, "validada": True, "acao_delete": None, "acao_update": None,
    }


def rel(nome: str, colunas: list[dict] | None = None, fks: list[dict] | None = None, pk: str | None = "id",
        tipo: str = "tabela", particao: dict | None = None) -> dict:
    cols = colunas if colunas is not None else []
    restricoes = list(fks or [])
    if pk is not None:
        restricoes.append({"nome": f"pk_{nome}", "tipo": "pk", "colunas": [pk]})
        if pk not in [c["nome"] for c in cols]:
            cols = [col(pk)] + cols
    return {"nome": nome, "tipo": tipo, "particao": particao, "colunas": cols, "restricoes": restricoes}


def montar(tmp_path: Path, schemas: dict[str, list[dict]], fatos: str | None = None) -> Path:
    doc = tmp_path / "doc"
    (doc / "config").mkdir(parents=True)
    (doc / "config" / "projeto.yml").write_text('projeto:\n  nome: "t"\n', encoding="utf-8")
    if fatos is not None:
        (doc / "config" / "fatos.yml").write_text(fatos, encoding="utf-8")
    pasta = doc / "catalogo" / "estrutura"
    pasta.mkdir(parents=True)
    for schema, relacoes in schemas.items():
        (pasta / f"{schema}.json").write_text(
            json.dumps({"formato": "estrutura", "schema": schema, "relacoes": relacoes}), encoding="utf-8"
        )
    return doc


def caminho_grafo(doc: Path) -> Path:
    return doc / "relacionamentos" / "grafo.json"


def rodar(doc: Path, etapa: str = "nome") -> dict[str, Any]:
    if etapa == "nome" and not caminho_grafo(doc).exists():
        assert cli.main(["relacionamentos", "fks", "--doc", str(doc)]) == 0
    assert cli.main(["relacionamentos", etapa, "--doc", str(doc)]) == 0
    return json.loads(caminho_grafo(doc).read_text(encoding="utf-8"))


def inferidas(grafo: dict) -> dict[str, dict]:
    return {a["id"]: a for a in grafo["arestas"] if a["fonte"] == "inferencia_nome"}


def base(extra_a: list[dict] | None = None, extra_b: list[dict] | None = None) -> dict[str, list[dict]]:
    """Uma FK qualquer (o grafo precisa de ao menos uma) mais as relações do teste."""
    ancora = rel("ancora")
    ligada = rel("ligada", [col("ancora_ref")], [fk("fk_a", ["ancora_ref"], "a", "ancora", ["id"])])
    return {"a": [ancora, ligada] + (extra_a or []), "b": extra_b or []}


# ----------------------------------------------------------------- regras e pontuação


def test_regra_exato_mesmo_schema_com_bonus_e_teto(tmp_path: Path) -> None:
    doc = montar(tmp_path, base([rel("conta"), rel("lote", [col("id_conta")])]))
    a = inferidas(rodar(doc))["nome:a.lote.id_conta->a.conta"]
    assert a["confianca"] == "hipotese" and a["taxa_casamento"] is None and a["nota"] is None
    assert a["origem"] == {"schema": "a", "relacao": "lote", "colunas": ["id_conta"]}
    assert a["destino"] == {"schema": "a", "relacao": "conta", "colunas": ["id"]}
    assert a["evidencia"] == [{"tipo": "inferencia_nome", "regra": "exato", "pontuacao": 1.0, "candidatos": 1}]


def test_exato_outro_schema_vale_1_e_modulo_vale_08_e_09_nao_existe(tmp_path: Path) -> None:
    doc = montar(
        tmp_path,
        {
            "a": [rel("ancora"), rel("p1", [col("conta_id")]), rel("p2", [col("id_peca")]),
                  rel("lig", [col("r")], [fk("f", ["r"], "a", "ancora", ["id"])])],
            "b": [rel("conta"), rel("mod_peca")],
            "c": [rel("p3", [col("id_item")]), rel("c_item")],
        },
    )
    grafo = rodar(doc)
    ev = {i: a["evidencia"][0] for i, a in inferidas(grafo).items()}
    assert ev["nome:a.p1.conta_id->b.conta"]["pontuacao"] == 1.0
    assert ev["nome:a.p2.id_peca->b.mod_peca"] == {"tipo": "inferencia_nome", "regra": "modulo", "pontuacao": 0.8, "candidatos": 1}
    # mesmo schema do destino: 0,8 + 0,2 = 1,0 (teto)
    assert ev["nome:c.p3.id_item->c.c_item"]["pontuacao"] == 1.0
    assert ev["nome:c.p3.id_item->c.c_item"]["regra"] == "modulo"


def test_modulo_pontua_menos_que_exato_e_exato_vence(tmp_path: Path) -> None:
    doc = montar(tmp_path, base([rel("grupo"), rel("t", [col("id_grupo")])], [rel("mod_grupo")]))
    assert list(inferidas(rodar(doc))) == ["nome:a.t.id_grupo->a.grupo"]


def test_modulo_no_mesmo_schema_empata_com_exato_de_outro(tmp_path: Path) -> None:
    doc = montar(tmp_path, base([rel("mod_grupo"), rel("t", [col("id_grupo")])], [rel("grupo")]))
    ids = inferidas(rodar(doc))
    assert sorted(ids) == ["nome:a.t.id_grupo->a.mod_grupo", "nome:a.t.id_grupo->b.grupo"]
    assert {i["evidencia"][0]["regra"] for i in ids.values()} == {"exato", "modulo"}


def test_underscore_inicial_conta_no_exato(tmp_path: Path) -> None:
    doc = montar(tmp_path, base([rel("_grupo"), rel("t", [col("id_grupo")])]))
    a = inferidas(rodar(doc))["nome:a.t.id_grupo->a._grupo"]
    assert a["evidencia"][0]["regra"] == "exato"


def test_comparacao_distingue_maiusculas(tmp_path: Path) -> None:
    doc = montar(tmp_path, base([rel("Grupo"), rel("t", [col("id_grupo")])]))
    assert inferidas(rodar(doc)) == {}


def test_qualificador_so_quando_faltam_exato_e_modulo(tmp_path: Path) -> None:
    doc = montar(
        tmp_path,
        base([rel("grupo"), rel("t1", [col("id_grupo_extra")]), rel("t2", [col("id_grupo")])], [rel("grupo_extra")]),
    )
    ids = inferidas(rodar(doc))
    # t1 tem exato (grupo_extra em b): qualificador não é avaliado
    assert "nome:a.t1.id_grupo_extra->b.grupo_extra" in ids
    assert ids["nome:a.t1.id_grupo_extra->b.grupo_extra"]["evidencia"][0]["regra"] == "exato"
    assert not [i for i in ids if "t1" in i and "a.grupo" in i]


def test_qualificador_reserva_pontua_05_e_bonus(tmp_path: Path) -> None:
    doc = montar(tmp_path, base([rel("grupo"), rel("t1", [col("id_grupo_extra")])], [rel("grupo")]))
    ids = inferidas(rodar(doc))
    assert ids["nome:a.t1.id_grupo_extra->a.grupo"]["evidencia"][0] == {
        "tipo": "inferencia_nome", "regra": "qualificador", "pontuacao": 0.7, "candidatos": 1,
    }
    assert len(ids) == 1  # o de b (0,5) perdeu para o de a (0,7)


def test_qualificador_usa_maior_prefixo(tmp_path: Path) -> None:
    doc = montar(tmp_path, base([rel("aa"), rel("aa_bb"), rel("t", [col("aa_bb_cc_id")])]))
    ids = inferidas(rodar(doc))
    assert list(ids) == ["nome:a.t.aa_bb_cc_id->a.aa_bb"]


# ----------------------------------------------------------------- tipo


def test_tipo_diferente_e_mesmo_tipo_base_nao_geram_candidato(tmp_path: Path) -> None:
    doc = montar(
        tmp_path,
        base([
            rel("alfa", [col("id", "integer")]),
            rel("beta", [col("id", "character varying(10)")]),
            rel("t", [col("id_alfa", BIGINT), col("id_beta", "character varying(20)")]),
        ]),
    )
    assert inferidas(rodar(doc)) == {}


def test_destino_precisa_de_pk_de_uma_coluna_e_nao_ser_view_nem_particao(tmp_path: Path) -> None:
    composta = rel("composta", [col("a1"), col("a2")], pk=None)
    composta["restricoes"].append({"nome": "pk_c", "tipo": "pk", "colunas": ["a1", "a2"]})
    doc = montar(
        tmp_path,
        base([
            composta,
            rel("sem_pk", pk=None),
            rel("visao", tipo="view"),
            rel("part", particao={"pai_schema": "a", "pai_nome": "pai", "limite": None}),
            rel("t", [col("id_composta"), col("id_sem_pk"), col("id_visao"), col("id_part")]),
        ]),
    )
    assert inferidas(rodar(doc)) == {}


def test_particionada_e_destino_valido_e_particao_nao_e_origem(tmp_path: Path) -> None:
    doc = montar(
        tmp_path,
        base([
            rel("pai", tipo="particionada"),
            rel("filha", [col("id_pai")], particao={"pai_schema": "a", "pai_nome": "x", "limite": None}),
            rel("t", [col("id_pai")]),
        ]),
    )
    assert list(inferidas(rodar(doc))) == ["nome:a.t.id_pai->a.pai"]


def test_view_nao_e_origem(tmp_path: Path) -> None:
    doc = montar(tmp_path, base([rel("grupo"), rel("v", [col("id_grupo")], pk=None, tipo="view")]))
    assert inferidas(rodar(doc)) == {}


def test_nomes_de_coluna_sem_x_nao_entram(tmp_path: Path) -> None:
    doc = montar(tmp_path, base([rel("id"), rel("t", [col("id"), col("id_"), col("_id")])]))
    assert inferidas(rodar(doc)) == {}
    assert relacionamentos_nome.extrair_x("id_a_id") == "a_id"
    assert relacionamentos_nome.extrair_x("a_b_id") == "a_b"


# ----------------------------------------------------------------- exclusões


def test_exclui_origem_de_fk_e_tenant(tmp_path: Path) -> None:
    esquemas = base([
        rel("grupo"),
        rel("t", [col("id_grupo"), col("id_dono"), col("id_outro")], [fk("fk_t", ["id_grupo"], "a", "grupo", ["id"])]),
        rel("dono"),
        rel("outro"),
    ])
    doc = montar(tmp_path, esquemas, 'tenant:\n  coluna: id_dono\n  valores:\n    x: "segredo"\n')
    grafo = rodar(doc)
    assert list(inferidas(grafo)) == ["nome:a.t.id_outro->a.outro"]
    b = grafo["resumo"]["inferencia_nome"]
    assert (b["excluidas_fk"], b["excluidas_tenant"], b["colunas_analisadas"]) == (1, 1, 1)
    assert "segredo" not in caminho_grafo(doc).read_text(encoding="utf-8")


def test_sem_tenant_declarado_nada_e_excluido(tmp_path: Path) -> None:
    esquemas = base([rel("dono"), rel("t", [col("id_dono")])])
    for fatos in (None, "outra: 1\n", "tenant:\n  descricao: x\n"):
        doc = montar(tmp_path / ("c" + str(abs(hash(fatos)))), esquemas, fatos)
        assert len(inferidas(rodar(doc))) == 1


def test_coluna_de_tenant_lida_so_de_coluna(tmp_path: Path) -> None:
    doc = montar(tmp_path, base(), 'tenant:\n  coluna: tn\n  valores: {a: 1}\n')
    assert relacionamentos_nome.coluna_de_tenant(doc) == "tn"


# ----------------------------------------------------------------- corte, empate e teto


def test_empate_gera_nota_e_candidatos(tmp_path: Path) -> None:
    doc = montar(tmp_path, base([rel("t", [col("id_grupo")])], [rel("grupo")]) | {"c": [rel("grupo")]})
    grafo = rodar(doc)
    ids = inferidas(grafo)
    assert sorted(ids) == ["nome:a.t.id_grupo->b.grupo", "nome:a.t.id_grupo->c.grupo"]
    for a in ids.values():
        assert a["nota"] == "ambíguo: 2 destinos possíveis"
        assert a["evidencia"][0]["candidatos"] == 2
    b = grafo["resumo"]["inferencia_nome"]
    assert (b["colunas_ambiguas"], b["colunas_com_candidato"], b["arestas"]) == (1, 1, 2)


def test_corte_fica_so_com_maior_pontuacao(tmp_path: Path) -> None:
    doc = montar(tmp_path, base([rel("grupo"), rel("t", [col("id_grupo")])], [rel("x_grupo")]))
    assert list(inferidas(rodar(doc))) == ["nome:a.t.id_grupo->a.grupo"]


def test_teto_de_20_destinos_registra_total(tmp_path: Path) -> None:
    schemas = {"a": [rel("ancora"), rel("t", [col("id_grupo")]),
                     rel("ligada", [col("r")], [fk("f", ["r"], "a", "ancora", ["id"])])]}
    for n in range(25):
        schemas[f"s{n:02d}"] = [rel("grupo")]
    grafo = rodar(montar(tmp_path, schemas))
    ids = sorted(inferidas(grafo))
    assert len(ids) == 20
    assert ids == sorted(f"nome:a.t.id_grupo->s{n:02d}.grupo" for n in range(20))
    a = inferidas(grafo)[ids[0]]
    assert a["evidencia"][0]["candidatos"] == 25 and a["nota"] == "ambíguo: 25 destinos possíveis"
    assert grafo["resumo"]["inferencia_nome"]["arestas"] == 20


def test_autorreferencia_permitida(tmp_path: Path) -> None:
    doc = montar(tmp_path, base([rel("no", [col("id_no")])]))
    assert list(inferidas(rodar(doc))) == ["nome:a.no.id_no->a.no"]


# ----------------------------------------------------------------- resumo e calibração


def test_resumo_por_regra_e_calibracao(tmp_path: Path) -> None:
    esquemas = {
        "a": [
            rel("ancora"),
            rel("ligada", [col("r")], [fk("f", ["r"], "a", "ancora", ["id"])]),
            rel("grupo"),
            rel("fa", [col("id_grupo")], [fk("fk_fa", ["id_grupo"], "a", "grupo", ["id"])]),  # acerto único
            rel("fb", [col("id_ancora")], [fk("fk_fb", ["id_ancora"], "a", "grupo", ["id"])]),  # erro
            rel("fc", [col("id_semdest")], [fk("fk_fc", ["id_semdest"], "a", "grupo", ["id"])]),  # sem candidato
            rel("fd", [col("id_dup")], [fk("fk_fd", ["id_dup"], "a", "m2_dup", ["id"])]),  # acerto ambíguo (modulo)
            rel("m1_dup"), rel("m2_dup"), rel("x_dup"),
            rel("fq", [col("id_grupo_z")], [fk("fk_fq", ["id_grupo_z"], "a", "grupo", ["id"])]),  # qualificador
            rel("novo", [col("id_ancora")]),
        ]
    }
    esquemas["a"].append(rel("fe", [col("id_dup")], [fk("fk_fe", ["id_dup"], "a", "m1_dup", ["id"])]))
    b = rodar(montar(tmp_path, esquemas))["resumo"]["inferencia_nome"]
    cal = b["calibracao_fk"]
    assert cal["sem_candidato"] == 1
    assert cal["por_regra"]["exato"] == {"acerto_unico": 1, "acerto_ambiguo": 0, "erro": 1, "repetem_fk": 1}
    assert cal["por_regra"]["modulo"] == {"acerto_unico": 0, "acerto_ambiguo": 2, "erro": 0, "repetem_fk": 2}
    assert cal["por_regra"]["qualificador"] == {"acerto_unico": 1, "acerto_ambiguo": 0, "erro": 0, "repetem_fk": 1}
    assert b["excluidas_fk"] == 6 and b["colunas_analisadas"] == 1 and b["arestas"] == 1
    assert b["por_regra"] == {"exato": 1, "modulo": 0, "qualificador": 0}
    assert b["excluidas_tenant"] == 0


def test_esquema_valida_bloco_e_rejeita_chave_extra(tmp_path: Path) -> None:
    grafo = rodar(montar(tmp_path, base([rel("g"), rel("t", [col("id_g")])])))
    relacionamentos.validar(grafo)
    ruim = json.loads(json.dumps(grafo))
    ruim["resumo"]["inferencia_nome"]["extra"] = 1
    with pytest.raises(ErroAtlas):
        relacionamentos.validar(ruim)


# ----------------------------------------------------------------- convivência com o fks


def test_nome_preserva_arestas_do_fks_e_fks_preserva_nome_e_bloco(tmp_path: Path) -> None:
    doc = montar(tmp_path, base([rel("g"), rel("t", [col("id_g")])]))
    grafo = rodar(doc)
    fks = [a for a in grafo["arestas"] if a["fonte"] == "fk_declarada"]
    assert len(fks) == 1 and grafo["resumo"]["por_fonte"] == {"fk_declarada": 1, "inferencia_nome": 1}
    bloco = grafo["resumo"]["inferencia_nome"]
    de_novo = rodar(doc, "fks")
    assert de_novo["resumo"]["inferencia_nome"] == bloco
    assert inferidas(de_novo) == inferidas(grafo)
    assert de_novo == grafo


def test_nome_preserva_arestas_de_terceira_fonte(tmp_path: Path) -> None:
    doc = montar(tmp_path, base([rel("g"), rel("t", [col("id_g")])]))
    grafo = rodar(doc)
    ext = {"schema": "a", "relacao": "g", "colunas": ["c"]}
    outra = {"id": "z:1", "origem": ext, "destino": ext, "confianca": "confirmado", "fonte": "amostragem",
             "taxa_casamento": 0.99, "nota": None, "evidencia": [{"tipo": "amostragem"}]}
    grafo["arestas"].append(outra)
    caminho_grafo(doc).write_text(json.dumps(grafo), encoding="utf-8")
    novo = rodar(doc)
    assert outra in novo["arestas"] and novo["resumo"]["total"] == 3


def test_nome_reescreve_so_as_proprias_arestas(tmp_path: Path) -> None:
    doc = montar(tmp_path, base([rel("g"), rel("t", [col("id_g")])]))
    grafo = rodar(doc)
    antiga = next(a for a in grafo["arestas"] if a["fonte"] == "inferencia_nome") | {"id": "nome:velha"}
    grafo["arestas"].append(antiga)
    caminho_grafo(doc).write_text(json.dumps(grafo), encoding="utf-8")
    novo = rodar(doc)
    assert "nome:velha" not in {a["id"] for a in novo["arestas"]}
    assert len(inferidas(novo)) == 1


def test_fks_avisa_quando_coluna_inferida_passou_a_ter_fk(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    esquemas = base([rel("g"), rel("t", [col("id_g")])])
    doc = montar(tmp_path, esquemas)
    rodar(doc)
    capsys.readouterr()
    esquemas["a"][-1]["restricoes"].append(fk("fk_novo", ["id_g"], "a", "g", ["id"]))
    montar(tmp_path / "x", esquemas)  # só para gerar o catálogo novo
    for f in (tmp_path / "x" / "doc" / "catalogo" / "estrutura").glob("*.json"):
        (doc / "catalogo" / "estrutura" / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    capsys.readouterr()
    rodar(doc, "fks")
    saida = capsys.readouterr()
    assert "nome:a.t.id_g->a.g" in saida.err and "reexecute relacionamentos nome" in saida.err
    assert len(inferidas(json.loads(caminho_grafo(doc).read_text(encoding="utf-8")))) == 1  # não apaga


# ----------------------------------------------------------------- determinismo e falhas


def test_determinismo_bytes_iguais(tmp_path: Path) -> None:
    doc = montar(tmp_path, base([rel("g"), rel("t", [col("id_g"), col("g_id")])], [rel("g")]))
    rodar(doc)
    primeiro = caminho_grafo(doc).read_bytes()
    rodar(doc)
    assert caminho_grafo(doc).read_bytes() == primeiro
    assert b"\r" not in primeiro and primeiro.endswith(b"\n")


def test_falha_sem_catalogo(tmp_path: Path) -> None:
    doc = tmp_path / "doc"
    (doc / "config").mkdir(parents=True)
    (doc / "config" / "projeto.yml").write_text('projeto:\n  nome: "t"\n', encoding="utf-8")
    assert cli.main(["relacionamentos", "nome", "--doc", str(doc)]) == 1


def test_falha_sem_grafo(tmp_path: Path) -> None:
    doc = montar(tmp_path, base())
    assert cli.main(["relacionamentos", "nome", "--doc", str(doc)]) == 1
    assert not caminho_grafo(doc).exists()


def test_falha_grafo_sem_fks(tmp_path: Path) -> None:
    doc = montar(tmp_path, base())
    texto = '{"versao_formato": 1, "arestas": [], "resumo": {"total": 0, "por_confianca": {}, "por_fonte": {}, "fk_declarada": {"autorreferencias": 0, "entre_schemas": 0, "not_valid": 0, "varias_colunas": 0}}}'
    caminho_grafo(doc).parent.mkdir()
    caminho_grafo(doc).write_text(texto, encoding="utf-8")
    assert cli.main(["relacionamentos", "nome", "--doc", str(doc)]) == 1
    assert caminho_grafo(doc).read_text(encoding="utf-8") == texto


def test_falha_grafo_invalido_sem_sobrescrever(tmp_path: Path) -> None:
    doc = montar(tmp_path, base())
    caminho_grafo(doc).parent.mkdir()
    caminho_grafo(doc).write_text('{"versao_formato": 1, "arestas": "x"}', encoding="utf-8")
    assert cli.main(["relacionamentos", "nome", "--doc", str(doc)]) == 1
    assert caminho_grafo(doc).read_text(encoding="utf-8") == '{"versao_formato": 1, "arestas": "x"}'


def test_falha_id_repetido_sem_gravar(tmp_path: Path) -> None:
    doc = montar(tmp_path, base([rel("g"), rel("t", [col("id_g")])]))
    grafo = rodar(doc)
    repetida = next(a for a in grafo["arestas"] if a["fonte"] == "inferencia_nome")
    grafo["arestas"].append(repetida | {"fonte": "amostragem"})  # mesmo id, outra fonte
    caminho_grafo(doc).write_text(json.dumps(grafo), encoding="utf-8")
    antes = caminho_grafo(doc).read_bytes()
    assert cli.main(["relacionamentos", "nome", "--doc", str(doc)]) == 1
    assert caminho_grafo(doc).read_bytes() == antes
