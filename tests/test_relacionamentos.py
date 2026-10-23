"""Testes offline das FKs declaradas no grafo (tarefa 2.1, D-016). Catálogo sintético."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pg_atlas import cli, relacionamentos
from pg_atlas.erros import ErroAtlas


def fk(nome: str, cols: list[str], ref_schema: str, ref_rel: str, ref_cols: list[str], validada: bool = True) -> dict:
    return {
        "nome": nome, "tipo": "fk", "colunas": cols, "ref_schema": ref_schema, "ref_relacao": ref_rel,
        "ref_colunas": ref_cols, "validada": validada, "acao_delete": "cascade", "acao_update": "no_action",
        "adiada": False, "adiavel": False, "definicao": None, "herdada": False, "tipo_match": "simple",
    }


def rel(nome: str, restricoes: list[dict] | None = None) -> dict:
    return {"nome": nome, "tipo": "tabela", "restricoes": restricoes or []}


def montar_doc(tmp_path: Path, schemas: dict[str, list[dict]]) -> Path:
    doc = tmp_path / "doc"
    (doc / "config").mkdir(parents=True)
    (doc / "config" / "projeto.yml").write_text('projeto:\n  nome: "t"\n', encoding="utf-8")
    pasta = doc / "catalogo" / "estrutura"
    pasta.mkdir(parents=True)
    for schema, relacoes in schemas.items():
        (pasta / f"{schema}.json").write_text(
            json.dumps({"formato": "estrutura", "schema": schema, "relacoes": relacoes}), encoding="utf-8"
        )
    return doc


def catalogo_basico(tmp_path: Path) -> Path:
    return montar_doc(
        tmp_path,
        {
            "a": [
                rel("pai"),
                rel("filho", [fk("fk_pai", ["pai_id"], "a", "pai", ["id"])]),
                rel("arvore", [fk("fk_acima", ["acima_id"], "a", "arvore", ["id"])]),
                rel(
                    "composta",
                    [fk("fk_comp", ["y", "x"], "b", "alvo", ["col_y", "col_x"]), fk("fk_nv", ["z"], "b", "alvo", ["k"], False)],
                ),
            ],
            "b": [rel("alvo")],
        },
    )


def rodar(doc: Path) -> dict[str, Any]:
    assert cli.main(["relacionamentos", "fks", "--doc", str(doc)]) == 0
    return json.loads((doc / "relacionamentos" / "grafo.json").read_text(encoding="utf-8"))


def test_fks_classificadas_e_resumo(tmp_path: Path) -> None:
    grafo = rodar(catalogo_basico(tmp_path))
    ids = [a["id"] for a in grafo["arestas"]]
    assert ids == sorted(ids) == ["fk:a.arvore.fk_acima", "fk:a.composta.fk_comp", "fk:a.composta.fk_nv", "fk:a.filho.fk_pai"]
    assert grafo["versao_formato"] == 1
    assert grafo["resumo"] == {
        "total": 4,
        "por_confianca": {"declarado": 4},
        "por_fonte": {"fk_declarada": 4},
        "fk_declarada": {"entre_schemas": 2, "autorreferencias": 1, "varias_colunas": 1, "not_valid": 1},
    }


def test_ordem_das_colunas_e_campos_da_aresta(tmp_path: Path) -> None:
    grafo = rodar(catalogo_basico(tmp_path))
    comp = next(a for a in grafo["arestas"] if a["id"].endswith("fk_comp"))
    assert comp["origem"] == {"schema": "a", "relacao": "composta", "colunas": ["y", "x"]}
    assert comp["destino"] == {"schema": "b", "relacao": "alvo", "colunas": ["col_y", "col_x"]}
    assert comp["confianca"] == "declarado" and comp["fonte"] == "fk_declarada"
    assert comp["taxa_casamento"] is None and comp["nota"] is None
    assert comp["evidencia"] == [
        {"tipo": "fk_declarada", "restricao": "fk_comp", "validada": True, "acao_delete": "cascade", "acao_update": "no_action"}
    ]


def test_not_valid_leva_nota_fixa(tmp_path: Path) -> None:
    grafo = rodar(catalogo_basico(tmp_path))
    nv = next(a for a in grafo["arestas"] if a["id"].endswith("fk_nv"))
    assert nv["confianca"] == "declarado"
    assert nv["nota"] == relacionamentos.NOTA_NOT_VALID
    assert nv["evidencia"][0]["validada"] is False


def test_determinismo_bytes_iguais(tmp_path: Path) -> None:
    doc = catalogo_basico(tmp_path)
    caminho = doc / "relacionamentos" / "grafo.json"
    rodar(doc)
    primeiro = caminho.read_bytes()
    rodar(doc)
    assert caminho.read_bytes() == primeiro
    assert b"\r" not in primeiro and primeiro.endswith(b"\n")


def outra_aresta() -> dict:
    ext = {"schema": "a", "relacao": "pai", "colunas": ["c"]}
    return {
        "id": "nome:a.pai.c", "origem": ext, "destino": {**ext, "relacao": "filho"}, "confianca": "hipotese",
        "fonte": "nome", "taxa_casamento": 0.5, "nota": None, "evidencia": [{"tipo": "nome"}],
    }


def test_preserva_arestas_de_outra_fonte(tmp_path: Path) -> None:
    doc = catalogo_basico(tmp_path)
    caminho = doc / "relacionamentos" / "grafo.json"
    grafo = rodar(doc)
    grafo["arestas"] = [a for a in grafo["arestas"] if a["id"].endswith("fk_pai")] + [outra_aresta()]
    caminho.write_text(json.dumps(grafo), encoding="utf-8")
    novo = rodar(doc)
    assert len(novo["arestas"]) == 5
    assert outra_aresta() in novo["arestas"]
    assert novo["resumo"]["por_fonte"] == {"fk_declarada": 4, "nome": 1}
    assert novo["resumo"]["por_confianca"] == {"declarado": 4, "hipotese": 1}
    assert novo["resumo"]["total"] == 5


def test_grafo_existente_invalido_falha_sem_sobrescrever(tmp_path: Path) -> None:
    doc = catalogo_basico(tmp_path)
    caminho = doc / "relacionamentos" / "grafo.json"
    caminho.parent.mkdir()
    caminho.write_text('{"versao_formato": 1, "arestas": "x"}', encoding="utf-8")
    with pytest.raises(ErroAtlas) as e:
        relacionamentos.executar_fks(doc)
    assert e.value.codigo_saida == 1
    assert caminho.read_text(encoding="utf-8") == '{"versao_formato": 1, "arestas": "x"}'


def test_grafo_existente_nao_json_falha(tmp_path: Path) -> None:
    doc = catalogo_basico(tmp_path)
    caminho = doc / "relacionamentos" / "grafo.json"
    caminho.parent.mkdir()
    caminho.write_text("{", encoding="utf-8")
    assert cli.main(["relacionamentos", "fks", "--doc", str(doc)]) == 1
    assert caminho.read_text(encoding="utf-8") == "{"


def test_id_duplicado_falha_sem_gravar(tmp_path: Path) -> None:
    doc = catalogo_basico(tmp_path)
    caminho = doc / "relacionamentos" / "grafo.json"
    rodar(doc)
    antes = caminho.read_bytes()
    grafo = json.loads(antes)
    extra = outra_aresta() | {"id": "fk:a.filho.fk_pai", "fonte": "nome"}
    grafo["arestas"].append(extra)
    caminho.write_text(json.dumps(grafo), encoding="utf-8")
    gravado = caminho.read_bytes()
    assert cli.main(["relacionamentos", "fks", "--doc", str(doc)]) == 1
    assert caminho.read_bytes() == gravado


def test_destino_ausente_falha_sem_gravar(tmp_path: Path) -> None:
    doc = montar_doc(tmp_path, {"a": [rel("filho", [fk("fk_x", ["c"], "a", "inexistente", ["id"])])]})
    assert cli.main(["relacionamentos", "fks", "--doc", str(doc)]) == 1
    assert not (doc / "relacionamentos" / "grafo.json").exists()


def test_numero_de_colunas_diferente_falha(tmp_path: Path) -> None:
    doc = montar_doc(
        tmp_path, {"a": [rel("pai"), rel("filho", [fk("fk_x", ["c", "d"], "a", "pai", ["id"])])]}
    )
    assert cli.main(["relacionamentos", "fks", "--doc", str(doc)]) == 1
    assert not (doc / "relacionamentos" / "grafo.json").exists()


def test_conferencia_de_contagem() -> None:
    aresta = relacionamentos.montar_aresta("a", "f", fk("k", ["c"], "a", "p", ["id"]))
    with pytest.raises(ErroAtlas):
        relacionamentos.conferir_arestas([aresta], {("a", "p")}, 2)


def test_catalogo_ausente_falha(tmp_path: Path) -> None:
    doc = tmp_path / "doc"
    (doc / "config").mkdir(parents=True)
    (doc / "config" / "projeto.yml").write_text('projeto:\n  nome: "t"\n', encoding="utf-8")
    assert cli.main(["relacionamentos", "fks", "--doc", str(doc)]) == 1


def test_esquema_aceita_outras_fontes_e_rejeita_confianca_invalida() -> None:
    base = {"versao_formato": 1, "arestas": [outra_aresta()], "resumo": relacionamentos.montar_resumo([outra_aresta()])}
    relacionamentos.validar(base)
    ruim = json.loads(json.dumps(base))
    ruim["arestas"][0]["confianca"] = "certo"
    with pytest.raises(ErroAtlas):
        relacionamentos.validar(ruim)
    ruim = json.loads(json.dumps(base))
    ruim["arestas"][0]["taxa_casamento"] = 1.5
    with pytest.raises(ErroAtlas):
        relacionamentos.validar(ruim)
