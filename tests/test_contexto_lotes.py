"""Testes offline da tarefa 3.1 (D-023): `contexto lotes`. Catálogo, grafo e lotes sintéticos e genéricos."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from pg_atlas import cli
from pg_atlas import contexto_lotes as ctx
from pg_atlas.erros import ErroAtlas
from pg_atlas.json_util import escrever_json
from pg_atlas.relacionamentos import montar_grafo, validar

FATOS = """\
tenant:
  coluna: id_tenant
  valores:
    - VALOR-SECRETO-DO-TENANT
cadastro:
  - "A tabela t1 tem uma regra de cadastro."
  - "Fato que não cita nada do lote."
"""


def rel(nome: str, tipo: str = "tabela", **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "nome": nome, "tipo": tipo, "acesso": "ok", "particao": None, "definicao": None, "comentario": None,
        "colunas": [
            {"nome": "id", "tipo": "bigint", "ordem": 1, "obrigatoria": True, "comentario": None, "padrao": None},
            {"nome": "k", "tipo": "text", "ordem": 2, "obrigatoria": False, "comentario": "coluna k", "padrao": "'abc'::text"},
        ],
        "restricoes": [{"nome": f"pk_{nome}", "tipo": "pk", "colunas": ["id"]}],
        "indices": [], "gatilhos": [], "depende_de": [],
    }
    base.update(extra)
    return base


def aresta(origem: str, destino: str, conf: str = "hipotese", taxa: float | None = None, fonte: str = "inferencia_nome") -> dict[str, Any]:
    so, ro = origem.split(".")
    sd, rd = destino.split(".")
    evidencia: list[dict[str, Any]] = [{"tipo": fonte, "regra": "exato", "pontuacao": 1.0, "candidatos": 1}]
    if fonte == "inferencia_codigo":
        evidencia = [{"tipo": fonte, "objetos": [{"schema": "s", "nome": "fn", "tipo": "function"}], "total_objetos": 1,
                      "tipos_iguais": True, "chave_sem_tenant": False}]
    if taxa is not None:
        evidencia.append({"tipo": "amostragem", "amostrados": 100, "casados": 90, "metodo": "direto", "pct": None, "filtro_tenant": "nenhum"})
    return {
        "id": f"{fonte}:{origem}->{destino}", "origem": {"schema": so, "relacao": ro, "colunas": ["id"]},
        "destino": {"schema": sd, "relacao": rd, "colunas": ["id"]}, "confianca": conf, "fonte": fonte,
        "taxa_casamento": taxa, "nota": None, "evidencia": evidencia,
    }


def montar_doc(tmp_path: Path, com_fatos: bool = True, aprovadas: list[str] | None = None) -> Path:
    doc = tmp_path / "doc"
    (doc / "config").mkdir(parents=True)
    (doc / "config" / "projeto.yml").write_text('projeto:\n  nome: "t"\n', encoding="utf-8")
    if com_fatos:
        (doc / "config" / "fatos.yml").write_text(FATOS, encoding="utf-8")
    lista = aprovadas if aprovadas is not None else ["s.dom", "s.fora_de_lote"]
    (doc / "config" / "dominios.yml").write_text("aprovadas:\n" + "".join(f"  - {a}\n" for a in lista), encoding="utf-8")
    t1 = rel("t1", restricoes=[
        {"nome": "pk_t1", "tipo": "pk", "colunas": ["id"]},
        {"nome": "ck_t1", "tipo": "check", "colunas": ["k"], "definicao": "CHECK (k <> 'segredo')"},
        {"nome": "fk_t1_t2", "tipo": "fk", "colunas": ["id"], "ref_schema": "s", "ref_relacao": "t2", "ref_colunas": ["id"]},
    ], indices=[{"nome": "ix_t1", "colunas": ["k"], "unico": False, "primario": False, "metodo": "btree", "predicado": "(k = 'xyz')"}],
        gatilhos=[{"nome": "tg", "momento": "BEFORE", "eventos": ["UPDATE", "INSERT"], "funcao_schema": "s", "funcao_nome": "fn_tg"}],
        comentario="x" * 2000)
    estrutura = {
        "formato": "estrutura", "schema": "s",
        "relacoes": [
            t1, rel("t2"), rel("dom"), rel("vz"), rel("tmp"),
            rel("v1", "view", definicao="SELECT * FROM s.t1 WHERE k = 'segredo' AND n = 123456789", depende_de=[{"schema": "s", "relacao": "t1", "tipo": "tabela"}]),
            rel("p1", particao={"pai_schema": "s", "pai_nome": "pai", "limite": "FOR VALUES IN ('uuid-secreto')"}),
        ],
        "funcoes": [],
    }
    escrever_json(doc / "catalogo" / "estrutura" / "s.json", estrutura)
    estatisticas = {"formato": "estatisticas", "relacoes": [
        {"nome": "t1", "linhas_estimadas": 500, "paginas": 5, "tamanho_total_bytes": 4096, "colunas": [
            {"nome": "k", "null_frac": 0.1, "n_distinct": -0.5, "avg_width": 4, "correlation": 0.2, "herdada": False,
             "most_common_vals": "{SEGREDO}", "histogram_bounds": "{SEGREDO}"},
            {"nome": "k", "null_frac": 0.9, "n_distinct": 1.0, "avg_width": 9, "correlation": None, "herdada": True},
        ]},
    ]}
    escrever_json(doc / "catalogo" / "estatisticas" / "s.json", estatisticas)
    grafo = montar_grafo([
        aresta("s.t1", "s.t2", "declarado", fonte="fk_declarada"),
        aresta("s.t1", "s.dom", "confirmado", 0.9),
        aresta("s.t2", "s.externa", "hipotese", 0.5),
        aresta("s.t2", "s.dom", "hipotese", None, "inferencia_codigo"),
        aresta("s.externa", "s.outra", "hipotese"),
    ], [])
    validar(grafo)
    escrever_json(doc / "relacionamentos" / "grafo.json", grafo)
    tabelas1 = [{"schema": "s", "nome": n, "classe": c} for n, c in (("dom", "dominio_candidata"), ("t1", "comum"), ("t2", "comum"))]
    lotes = [
        {"id": "lote-001", "tipo": "grafo", "classe_leve": None, "schemas": ["s"], "tabelas": tabelas1, "total_tabelas": 3},
        {"id": "leve-vazia-001", "tipo": "leve", "classe_leve": "vazia", "schemas": ["s"], "total_tabelas": 1,
         "tabelas": [{"schema": "s", "nome": "vz", "classe": "vazia"}]},
        {"id": "leve-temporaria-001", "tipo": "leve", "classe_leve": "temporaria_ou_copia", "schemas": ["s"], "total_tabelas": 1,
         "tabelas": [{"schema": "s", "nome": "tmp", "classe": "temporaria_ou_copia"}]},
    ]
    escrever_json(doc / "relacionamentos" / "lotes.json", {"lotes": lotes})
    return doc


def rodar(doc: Path) -> int:
    return cli.main(["contexto", "lotes", "--doc", str(doc)])


def ler(doc: Path, nome: str) -> Any:
    return json.loads((doc / "_controle" / "lotes" / nome).read_text(encoding="utf-8"))


def test_gera_um_pacote_por_lote_e_indice(tmp_path: Path) -> None:
    doc = montar_doc(tmp_path)
    assert rodar(doc) == 0
    nomes = sorted(p.name for p in (doc / "_controle" / "lotes").iterdir())
    assert nomes == ["contexto-indice.json", "leve-temporaria-001.json", "leve-vazia-001.json", "lote-001.json"]
    indice = ler(doc, "contexto-indice.json")
    assert indice["resumo"]["total_lotes"] == 3 and indice["resumo"]["total_tabelas"] == 5
    assert indice["resumo"]["maior_pacote"]["lote"] == "lote-001"
    assert indice["resumo"]["dominios_pendentes"] == 1 and indice["resumo"]["dominios_aprovados_sem_lote"] == 1


def test_nao_toca_arquivos_do_analista_de_relacionamentos(tmp_path: Path) -> None:
    doc = montar_doc(tmp_path)
    pasta = doc / "_controle" / "lotes"
    pasta.mkdir(parents=True)
    for nome in ("relacionamentos-p1-001.json", "relacionamentos-indice.json", "relacionamentos-p1-001.resolvido.json"):
        (pasta / nome).write_text("original", encoding="utf-8")
    rodar(doc)
    assert all((pasta / n).read_text(encoding="utf-8") == "original" for n in (
        "relacionamentos-p1-001.json", "relacionamentos-indice.json", "relacionamentos-p1-001.resolvido.json"))


def test_pacote_do_grafo_tem_estrutura_e_so_estatisticas_permitidas(tmp_path: Path) -> None:
    doc = montar_doc(tmp_path)
    rodar(doc)
    texto = (doc / "_controle" / "lotes" / "lote-001.json").read_text(encoding="utf-8")
    for proibido in ("most_common", "histogram", "SEGREDO", "VALOR-SECRETO-DO-TENANT", "segredo", "uuid-secreto", "123456789"):
        assert proibido not in texto
    pacote = json.loads(texto)
    t1 = next(t for t in pacote["tabelas"] if t["nome"] == "t1")
    k = next(c for c in t1["colunas"] if c["nome"] == "k")
    assert k["estatisticas"] == {"avg_width": 4, "correlation": 0.2, "herdada": False, "n_distinct": -0.5, "null_frac": 0.1}
    assert k["padrao"] == "'?'::text"
    assert t1["pk"] == ["id"] and t1["linhas_estimadas"] == 500
    assert t1["fks_declaradas"][0]["referencia"] == "s.t2"
    assert t1["gatilhos"] == [{"eventos": ["INSERT", "UPDATE"], "funcao": "s.fn_tg", "momento": "BEFORE", "nome": "tg"}]
    assert t1["checks"][0]["definicao"] == "CHECK (k <> '?')"
    assert len(t1["comentario"]) < 700 and t1["comentario"].endswith("[cortado]")
    assert pacote["fatos"] == ["A tabela t1 tem uma regra de cadastro."]


def test_relacionamentos_usos_e_dominios(tmp_path: Path) -> None:
    doc = montar_doc(tmp_path)
    rodar(doc)
    pacote = ler(doc, "lote-001.json")
    ids = [a["id"] for a in pacote["relacionamentos"]]
    assert ids == sorted(ids) and "inferencia_nome:s.externa->s.outra" not in ids  # não toca o lote
    por_id = {a["id"]: a for a in pacote["relacionamentos"]}
    assert por_id["fk_declarada:s.t1->s.t2"]["posicao"] == "interna"
    assert por_id["inferencia_nome:s.t2->s.externa"]["posicao"] == "saida"
    assert por_id["inferencia_nome:s.t1->s.dom"]["amostrados"] == 100
    assert pacote["usos"]["views"] == [{"nome": "s.v1", "tipo": "view"}]
    assert pacote["usos"]["rotinas"] == [
        {"motivo": "codigo", "nome": "s.fn", "tipo": "function"}, {"motivo": "gatilho", "nome": "s.fn_tg", "tipo": "function"},
    ]
    assert pacote["dominios"] == [{"situacao": "pendente", "tabela": "s.dom", "valores": None}]


def test_view_e_particao_sao_sanitizadas(tmp_path: Path) -> None:
    doc = montar_doc(tmp_path)
    fontes = ctx.carregar_fontes(doc)
    v = ctx.montar_tabela({"schema": "s", "nome": "v1", "classe": "comum"}, fontes)
    assert v["definicao"] == "SELECT * FROM s.t1 WHERE k = '?' AND n = 0" and v["depende_de"] == ["s.t1"]
    p = ctx.montar_tabela({"schema": "s", "nome": "p1", "classe": "particao", "particao_de": "s.pai"}, fontes)
    assert p["particao"] == {"limite": "FOR VALUES IN ('?')", "pai": "s.pai"}


def test_pacote_leve_e_reduzido(tmp_path: Path) -> None:
    doc = montar_doc(tmp_path)
    rodar(doc)
    pacote = ler(doc, "leve-vazia-001.json")
    assert pacote["tabelas"] == [{"classe": "vazia", "comentario": None, "nome": "vz", "schema": "s", "tipo": "tabela", "total_colunas": 2}]
    assert "relacionamentos" not in pacote and "dominios" not in pacote


def test_sem_fatos_ou_dominios_nao_falha(tmp_path: Path) -> None:
    doc = montar_doc(tmp_path, com_fatos=False, aprovadas=[])
    assert rodar(doc) == 0
    pacote = ler(doc, "lote-001.json")
    assert pacote["fatos"] == [] and pacote["dominios"] == []
    (doc / "config" / "dominios.yml").unlink()
    assert rodar(doc) == 0


def test_deterministico_e_remove_pacote_obsoleto(tmp_path: Path) -> None:
    doc = montar_doc(tmp_path)
    rodar(doc)
    pasta = doc / "_controle" / "lotes"
    antes = {p.name: p.read_bytes() for p in pasta.iterdir()}
    assert all(b"\r\n" not in b for b in antes.values())
    (pasta / "lote-099.json").write_text("{}", encoding="utf-8")  # lote que não existe mais
    rodar(doc)
    assert {p.name: p.read_bytes() for p in pasta.iterdir()} == antes


def test_tabela_em_dois_lotes_falha_sem_gravar(tmp_path: Path) -> None:
    doc = montar_doc(tmp_path)
    lotes = json.loads((doc / "relacionamentos" / "lotes.json").read_text(encoding="utf-8"))
    lotes["lotes"][1]["tabelas"].append({"schema": "s", "nome": "t1", "classe": "vazia"})
    escrever_json(doc / "relacionamentos" / "lotes.json", lotes)
    with pytest.raises(ErroAtlas):
        ctx.gerar(doc)
    assert not (doc / "_controle" / "lotes").exists()


def test_esquema_recusa_estatistica_proibida() -> None:
    estat = ctx._esquema("contexto-lote.schema.json")["$defs"]["estatisticas"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"null_frac": 0.1, "most_common_vals": "{x}"}, estat)


def test_cortes_de_tamanho_registram_truncamento(tmp_path: Path) -> None:
    doc = montar_doc(tmp_path)
    fontes = ctx.carregar_fontes(doc)
    lote = fontes.lotes["lotes"][0]
    pacote = ctx.montar_pacote_grafo(lote, fontes)
    assert pacote["truncamento"] == {}
    cortado = ctx.aplicar_cortes(pacote, max_bytes=1)
    assert set(cortado["truncamento"]) == {
        "definicoes_omitidas", "hipoteses_omitidas_por_tamanho", "externas_cortadas_por_tamanho", "comentarios_de_coluna_cortados"}
    assert all(a["confianca"] != "hipotese" for a in cortado["relacionamentos"])


def test_tetos_de_arestas() -> None:
    dentro = {"s.a"}
    arestas = [aresta("s.a", f"s.x{i}", "declarado", fonte="fk_declarada") for i in range(5)]
    arestas += [aresta("s.a", f"s.h{i}", "hipotese", taxa=i / 10) for i in range(5)]
    escolhidas, omitidas = ctx.selecionar_arestas(arestas, dentro, max_hipoteses=2, max_externas=3)
    assert omitidas == {"externas_omitidas_pelo_teto": 2, "hipoteses_omitidas_pelo_teto": 3}
    hip = [a["id"] for a in escolhidas if a["confianca"] == "hipotese"]
    assert hip == ["inferencia_nome:s.a->s.h3", "inferencia_nome:s.a->s.h4"]  # maiores taxas
