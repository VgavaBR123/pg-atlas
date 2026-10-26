"""Testes offline da tarefa 2.6 (D-021): classificação, lotes, `resolver`. Catálogo e grafo sintéticos e genéricos."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pg_atlas import cli
from pg_atlas import relacionamentos_lotes as lotes
from pg_atlas.json_util import escrever_json
from pg_atlas.relacionamentos import montar_grafo, validar
from pg_atlas.verificar_aplicar import restaurar_base

BIGINT = "bigint"
FATOS = """\
tenant:
  coluna: id_tenant
  valores:
    - VALOR-SECRETO-DO-TENANT
cadastro:
  - "A coluna id_y é preenchida pelo cadastro."
"""
CODIGO_FN = (
    "CREATE FUNCTION s.fn() RETURNS void AS $function$\nBEGIN\n"
    "  SELECT 1 FROM s.fato2 f JOIN s.alvo_a a ON a.id = f.id_y WHERE f.k = 'segredo' AND f.n = 123456789;\n"
    "END;\n$function$"
)


# ----------------------------------------------------------------- montagem


def tabela(nome: str, colunas: list[str], comentarios: dict[str, str] | None = None) -> dict[str, Any]:
    cs = ["id", *colunas]
    return {
        "nome": nome, "tipo": "tabela", "acesso": "ok", "particao": None, "definicao": None,
        "comentario": (comentarios or {}).get("_tabela"),
        "colunas": [
            {"nome": c, "tipo": BIGINT, "ordem": i, "comentario": (comentarios or {}).get(c)} for i, c in enumerate(cs)
        ],
        "restricoes": [{"nome": f"pk_{nome}", "tipo": "pk", "colunas": ["id"]}],
        "indices": [],
    }


def aresta(
    origem: str, destino: str, *, fonte: str = "inferencia_nome", confianca: str = "hipotese",
    taxa: float | None = None, amostrados: int = 1000, nota: str | None = "ambíguo: 2 destinos possíveis",
) -> dict[str, Any]:
    prefixo = "nome" if fonte == "inferencia_nome" else "codigo"
    evidencia: list[dict[str, Any]]
    if fonte == "inferencia_nome":
        evidencia = [{"tipo": fonte, "regra": "exato", "pontuacao": 1.0, "candidatos": 2}]
    else:
        evidencia = [{
            "tipo": fonte, "objetos": [{"schema": "s", "nome": "fn", "tipo": "function"}], "total_objetos": 1,
            "tipos_iguais": True, "chave_sem_tenant": False,
        }]
    if taxa is not None:
        evidencia.append({
            "tipo": "amostragem", "amostrados": amostrados, "casados": int(taxa * amostrados),
            "metodo": "direto", "pct": None, "filtro_tenant": "nenhum",
        })
    rel_o, col = origem.rsplit(".", 1)
    s_o, r_o = rel_o.split(".")
    s_d, r_d = destino.split(".")
    return {
        "id": f"{prefixo}:{origem}->{destino}",
        "origem": {"schema": s_o, "relacao": r_o, "colunas": [col]},
        "destino": {"schema": s_d, "relacao": r_d, "colunas": ["id"]},
        "confianca": confianca, "fonte": fonte, "taxa_casamento": taxa, "nota": nota, "evidencia": evidencia,
    }


def arestas_base() -> list[dict[str, Any]]:
    return [
        # coluna 1: taxa útil (P1)
        aresta("s.fato1.id_x", "s.alvo_a", confianca="confirmado", taxa=1.0),
        aresta("s.fato1.id_x", "s.alvo_b", taxa=0.8),
        aresta("s.fato1.id_x", "s.alvo_c", taxa=0.6),
        # coluna 2: sem taxa, com código e comentário (P2)
        aresta("s.fato2.id_y", "s.alvo_a"),
        aresta("s.fato2.id_y", "s.alvo_b"),
        aresta("s.fato2.id_y", "s.alvo_a", fonte="inferencia_codigo", nota=None),
        # coluna 3: nada (P3)
        aresta("s.fato3.id_x", "s.alvo_a", nota="ambíguo: 2 destinos possíveis; sem verificação: sem permissão de leitura"),
        aresta("s.fato3.id_x", "s.alvo_b", nota="ambíguo: 2 destinos possíveis; sem verificação: sem permissão de leitura"),
        # fora da fila: sem nota de ambiguidade
        aresta("s.fato4.id_x", "s.alvo_a", nota=None),
    ]


def montar_doc(tmp_path: Path, arestas: list[dict[str, Any]] | None = None, fatos: str | None = FATOS) -> Path:
    doc = tmp_path / "doc"
    (doc / "config").mkdir(parents=True)
    (doc / "config" / "projeto.yml").write_text('projeto:\n  nome: "t"\n', encoding="utf-8")
    if fatos is not None:
        (doc / "config" / "fatos.yml").write_text(fatos, encoding="utf-8")
    pasta = doc / "catalogo" / "estrutura"
    pasta.mkdir(parents=True)
    relacoes = [
        tabela("fato1", ["id_x"]),
        tabela("fato2", ["id_y", "k"], {"_tabela": "Fatos do segundo tipo", "id_y": "referência ao alvo_b ou ao outro"}),
        tabela("fato3", ["id_x"]),
        tabela("fato4", ["id_x"]),
        tabela("alvo_a", []),
        tabela("alvo_b", []),
        tabela("alvo_c", []),
    ]
    funcoes = [{"nome": "fn", "tipo": "function", "linguagem": "sql", "codigo": CODIGO_FN}]
    escrever_json(pasta / "s.json", {"formato": "estrutura", "schema": "s", "relacoes": relacoes, "funcoes": funcoes})
    grafo = montar_grafo(arestas if arestas is not None else arestas_base(), [])
    validar(grafo)
    escrever_json(doc / "relacionamentos" / "grafo.json", grafo)
    return doc


def rodar(*args: str) -> int:
    return cli.main(["relacionamentos", *args])


def ler(doc: Path, *partes: str) -> Any:
    return json.loads(doc.joinpath(*partes).read_text(encoding="utf-8"))


def grafo_de(doc: Path) -> dict[str, Any]:
    return ler(doc, "relacionamentos", "grafo.json")


def por_id(doc: Path) -> dict[str, dict[str, Any]]:
    return {a["id"]: a for a in grafo_de(doc)["arestas"]}


def escrever_resolvido(doc: Path, lote: str, colunas: list[dict[str, Any]]) -> Path:
    arquivo = doc / "_controle" / "lotes" / f"relacionamentos-{lote}.resolvido.json"
    escrever_json(arquivo, {"formato": "relacionamentos_resolvido", "versao_formato": 1, "lote": lote, "colunas": colunas})
    return arquivo


def destino(aresta_id: str, acao: str, **extra: Any) -> dict[str, Any]:
    return {"aresta": aresta_id, "acao": acao, "justificativa": "porque sim", **extra}


COL1 = "s.fato1.id_x"
A1 = "nome:s.fato1.id_x->s.alvo_a"
B1 = "nome:s.fato1.id_x->s.alvo_b"
C1 = "nome:s.fato1.id_x->s.alvo_c"
COL2 = "s.fato2.id_y"
A2 = "nome:s.fato2.id_y->s.alvo_a"
B2 = "nome:s.fato2.id_y->s.alvo_b"


# ----------------------------------------------------------------- sanitização


def test_sanitizar_strings_e_numeros() -> None:
    texto = "WHERE a = 'segredo' AND b = 'it''s' AND n = 123456 AND m = 1234 AND x = 12345.678 -- obs 999999"
    saida = lotes.sanitizar_codigo(texto)
    assert "segredo" not in saida
    assert saida == "WHERE a = '?' AND b = '?' AND n = 0 AND m = 1234 AND x = 0.678 -- obs 0"


def test_sanitizar_mantem_corpo_da_rotina_e_troca_outras_dollar_strings() -> None:
    texto = "AS $function$\nBEGIN x := $q$valor secreto$q$; y := tabela12345; END;\n$function$"
    saida = lotes.sanitizar_codigo(texto)
    assert saida.startswith("AS $function$") and saida.endswith("$function$")
    assert "secreto" not in saida
    assert "tabela12345" in saida  # dígitos dentro de identificador ficam


def test_sanitizar_escape_e_identificador_com_aspas() -> None:
    saida = lotes.sanitizar_codigo("select E'a\\'b segredo', \"Col 99999\" from t")
    assert "segredo" not in saida and '"Col 99999"' in saida


# ----------------------------------------------------------------- classificação


def test_classificacao_p1_p2_p3(tmp_path: Path) -> None:
    doc = montar_doc(tmp_path)
    ctx = lotes.montar_contexto(doc, 30)
    grupos = lotes.agrupar_ambiguas(grafo_de(doc)["arestas"])
    assert [lotes.id_coluna(k) for k in grupos] == [COL1, COL2, "s.fato3.id_x"]  # fato4 não tem nota
    niveis = lotes.classificar(grupos, ctx)
    assert [lotes.id_coluna(k) for k in niveis["p1"]] == [COL1]
    assert [lotes.id_coluna(k) for k in niveis["p2"]] == [COL2]
    assert [lotes.id_coluna(k) for k in niveis["p3"]] == ["s.fato3.id_x"]
    chave2 = niveis["p2"][0]
    assert lotes.motivos_p2(chave2, grupos[chave2], ctx) == ["codigo", "comentario", "fato"]
    chave3 = niveis["p3"][0]
    assert lotes.motivo_p3(chave3, grupos[chave3], ctx) == "sem_select"


def test_amostra_pequena_nao_e_taxa_util(tmp_path: Path) -> None:
    arestas = [
        aresta("s.fato1.id_x", "s.alvo_a", taxa=1.0, amostrados=10, nota="ambíguo: 2 destinos possíveis; amostra pequena (10 valores)"),
        aresta("s.fato1.id_x", "s.alvo_b", taxa=1.0, amostrados=10, nota="ambíguo: 2 destinos possíveis; amostra pequena (10 valores)"),
    ]
    doc = montar_doc(tmp_path, arestas, fatos=None)
    ctx = lotes.montar_contexto(doc, 30)
    niveis = lotes.classificar(lotes.agrupar_ambiguas(grafo_de(doc)["arestas"]), ctx)
    assert niveis["p1"] == [] and len(niveis["p3"]) == 1
    grupos = lotes.agrupar_ambiguas(grafo_de(doc)["arestas"])
    assert lotes.motivo_p3(niveis["p3"][0], grupos[niveis["p3"][0]], ctx) == "amostra_pequena"


def test_fatos_ignoram_tenant(tmp_path: Path) -> None:
    doc = montar_doc(tmp_path)
    assert lotes.ler_fatos(doc) == ["A coluna id_y é preenchida pelo cadastro."]


# ----------------------------------------------------------------- lotes


def test_lotes_geram_arquivos_indice_e_p3(tmp_path: Path) -> None:
    doc = montar_doc(tmp_path)
    assert rodar("lotes", "--doc", str(doc)) == 0
    pasta = doc / "_controle" / "lotes"
    assert sorted(p.name for p in pasta.iterdir()) == [
        "relacionamentos-indice.json", "relacionamentos-p1-001.json", "relacionamentos-p2-001.json",
    ]
    indice = ler(doc, "_controle", "lotes", "relacionamentos-indice.json")
    assert indice["resumo"]["colunas_fila"] == {"p1": 1, "p2": 1, "p3": 1}
    assert [(l["lote"], l["nivel"], l["colunas"], l["situacao"]) for l in indice["lotes"]] == [
        ("p1-001", "p1", 1, "pendente"), ("p2-001", "p2", 1, "pendente"),
    ]
    p3 = ler(doc, "_controle", "relacionamentos-sem-evidencia.json")
    assert p3["total"] == 1 and p3["colunas"][0]["id_coluna"] == "s.fato3.id_x"
    assert p3["colunas"][0]["motivo"] == "sem_select"
    assert [d["relacao"] for d in p3["colunas"][0]["destinos"]] == ["alvo_a", "alvo_b"]


def test_lote_leva_so_metadado_e_trechos_sanitizados(tmp_path: Path) -> None:
    doc = montar_doc(tmp_path)
    rodar("lotes", "--doc", str(doc))
    texto = (doc / "_controle" / "lotes" / "relacionamentos-p2-001.json").read_text(encoding="utf-8")
    assert "VALOR-SECRETO-DO-TENANT" not in texto
    assert "segredo" not in texto and "123456789" not in texto
    lote = json.loads(texto)
    assert lote["formato_saida"]["estrutura"]["formato"] == "relacionamentos_resolvido"
    coluna = lote["colunas"][0]
    assert coluna["id_coluna"] == COL2 and coluna["motivos"] == ["codigo", "comentario", "fato"]
    assert coluna["fatos"] == ["A coluna id_y é preenchida pelo cadastro."]
    com_trecho = [d for d in coluna["destinos"] if d["trechos"]]
    assert len(com_trecho) == 1
    linhas = com_trecho[0]["trechos"][0]["linhas"]
    assert any("f.k = '?'" in l and "f.n = 0" in l for l in linhas)
    assert [d["aresta"] for d in coluna["destinos"]] == sorted(d["aresta"] for d in coluna["destinos"])


def test_lotes_deterministicos_e_limites(tmp_path: Path) -> None:
    arestas = []
    for i in range(30):  # 30 colunas P1, mais que o teto de 25 por lote
        for destino_ in ("alvo_a", "alvo_b"):
            arestas.append(aresta(f"s.fato1.id_c{i:02d}", f"s.{destino_}", taxa=0.9))
    doc = montar_doc(tmp_path, arestas)
    rodar("lotes", "--doc", str(doc))
    pasta = doc / "_controle" / "lotes"
    antes = {p.name: p.read_bytes() for p in pasta.iterdir()}
    indice = json.loads(antes["relacionamentos-indice.json"])
    assert [(l["lote"], l["colunas"]) for l in indice["lotes"]] == [("p1-001", 25), ("p1-002", 5)]
    rodar("lotes", "--doc", str(doc))
    assert {p.name: p.read_bytes() for p in pasta.iterdir()} == antes


def test_ordem_pelas_linhas_estimadas(tmp_path: Path) -> None:
    doc = montar_doc(tmp_path, [
        aresta("s.fato1.id_x", "s.alvo_a", taxa=0.9), aresta("s.fato1.id_x", "s.alvo_b", taxa=0.9),
        aresta("s.fato3.id_x", "s.alvo_a", taxa=0.9), aresta("s.fato3.id_x", "s.alvo_b", taxa=0.9),
    ])
    estat = doc / "catalogo" / "estatisticas"
    estat.mkdir()
    escrever_json(estat / "s.json", {"relacoes": [
        {"nome": "fato1", "linhas_estimadas": 10, "paginas": 1},
        {"nome": "fato3", "linhas_estimadas": 5000, "paginas": 10},
    ]})
    ctx = lotes.montar_contexto(doc, 30)
    niveis = lotes.classificar(lotes.agrupar_ambiguas(grafo_de(doc)["arestas"]), ctx)
    assert [lotes.id_coluna(k) for k in niveis["p1"]] == ["s.fato3.id_x", COL1]


def test_lote_muito_grande_corta_trechos() -> None:
    colunas = [{"id_coluna": f"c{i}", "x": "y" * 40_000} for i in range(10)]
    blocos = lotes.dividir(colunas)
    assert all(len(b) <= lotes.MAX_COLUNAS for b in blocos)
    assert all(lotes.tamanho({"colunas": b}) <= lotes.MAX_BYTES + 4_000 for b in blocos)
    assert sum(len(b) for b in blocos) == 10


def test_lote_resolvido_nao_e_sobrescrito_nem_recolocado(tmp_path: Path) -> None:
    doc = montar_doc(tmp_path)
    rodar("lotes", "--doc", str(doc))
    arquivo = doc / "_controle" / "lotes" / "relacionamentos-p1-001.json"
    escrever_resolvido(doc, "p1-001", [{"coluna": COL1, "resultado": "duvida", "justificativa": "sem base"}])
    conteudo = arquivo.read_bytes()
    rodar("lotes", "--doc", str(doc))
    assert arquivo.read_bytes() == conteudo
    indice = ler(doc, "_controle", "lotes", "relacionamentos-indice.json")
    assert [(l["lote"], l["situacao"]) for l in indice["lotes"]] == [("p1-001", "resolvido"), ("p2-001", "pendente")]
    assert indice["resumo"]["colunas_fila"]["p1"] == 1
    rodar("lotes", "--doc", str(doc), "--refazer")
    assert ler(doc, "_controle", "lotes", "relacionamentos-indice.json")["lotes"][0]["situacao"] == "pendente"


def test_lotes_exigem_grafo(tmp_path: Path) -> None:
    doc = montar_doc(tmp_path)
    (doc / "relacionamentos" / "grafo.json").unlink()
    assert rodar("lotes", "--doc", str(doc)) == 1


# ----------------------------------------------------------------- resolver


def preparar_resolver(tmp_path: Path) -> Path:
    doc = montar_doc(tmp_path)
    rodar("lotes", "--doc", str(doc))
    return doc


def test_resolver_exige_indice(tmp_path: Path) -> None:
    doc = montar_doc(tmp_path)
    assert rodar("resolver", "--doc", str(doc)) == 1


def test_escolhido_preterido_e_descartado(tmp_path: Path) -> None:
    doc = preparar_resolver(tmp_path)
    escrever_resolvido(doc, "p1-001", [{
        "coluna": COL1, "resultado": "escolhido", "justificativa": "código liga a coluna ao alvo_a",
        "destinos": [destino(A1, "escolhido", grau="confirmado"), destino(B1, "preterido"), destino(C1, "descartado")],
    }])
    assert rodar("resolver", "--doc", str(doc)) == 0
    g = por_id(doc)
    assert g[A1]["nota"] is None and g[A1]["confianca"] == "confirmado"
    assert g[A1]["evidencia"][-1]["resultado"] == "escolhido" and g[A1]["evidencia"][-1]["lote"] == "p1-001"
    assert g[B1]["confianca"] == "hipotese" and g[B1]["taxa_casamento"] == 0.8
    assert g[B1]["nota"] == f"preterido pelo analista: escolhido {A1}"
    assert C1 in g and g[C1]["taxa_casamento"] == 0.6  # taxa útil >= 0,50: descarte vira preterido
    assert g[C1]["nota"].startswith("preterido pelo analista")
    a = grafo_de(doc)["resumo"]["analise"]
    assert (a["escolhidas"], a["arestas_preteridas"], a["arestas_descartadas"], a["descarte_convertido"]) == (1, 2, 0, 1)
    assert a["colunas_fila"] == {"p1": 1, "p2": 1, "p3": 1} and a["lotes"] == {"gerados": 2, "resolvidos": 1}
    assert a["sem_evidencia"] == 1 and a["grau_ajustado"] == 0 and a["itens_rejeitados"] == 0
    assert ler(doc, "_controle", "lotes", "relacionamentos-indice.json")["lotes"][0]["situacao"] == "resolvido"


def test_confirmado_rebaixado_ao_ser_preterido(tmp_path: Path) -> None:
    doc = preparar_resolver(tmp_path)
    escrever_resolvido(doc, "p1-001", [{
        "coluna": COL1, "resultado": "escolhido", "justificativa": "o alvo_b é o pai",
        "destinos": [destino(B1, "escolhido")],
    }])
    rodar("resolver", "--doc", str(doc))
    g = por_id(doc)
    assert g[A1]["confianca"] == "hipotese" and g[A1]["taxa_casamento"] == 1.0
    assert g[B1]["confianca"] == "hipotese"  # a escolha não promove


def test_descarte_sem_taxa_remove_aresta(tmp_path: Path) -> None:
    doc = preparar_resolver(tmp_path)
    escrever_resolvido(doc, "p2-001", [{
        "coluna": COL2, "resultado": "escolhido", "justificativa": "o código liga ao alvo_a",
        "destinos": [destino(A2, "escolhido"), destino(B2, "descartado")],
    }])
    rodar("resolver", "--doc", str(doc))
    g = por_id(doc)
    assert B2 not in g and g[A2]["nota"] is None
    assert grafo_de(doc)["resumo"]["analise"]["arestas_descartadas"] == 1
    assert "codigo:s.fato2.id_y->s.alvo_a" in g  # arestas de código não citadas viram preteridas
    assert g["codigo:s.fato2.id_y->s.alvo_a"]["nota"].startswith("preterido pelo analista")


def test_grau_confirmado_sem_taxa_nao_promove(tmp_path: Path) -> None:
    doc = preparar_resolver(tmp_path)
    escrever_resolvido(doc, "p2-001", [{
        "coluna": COL2, "resultado": "escolhido", "justificativa": "palpite embasado no comentário",
        "destinos": [destino(B2, "escolhido", grau="confirmado")],
    }])
    rodar("resolver", "--doc", str(doc))
    g = por_id(doc)
    assert g[B2]["confianca"] == "hipotese" and g[B2]["taxa_casamento"] is None
    assert grafo_de(doc)["resumo"]["analise"]["grau_ajustado"] == 1
    for a in grafo_de(doc)["arestas"]:  # invariante: confirmado sempre com taxa
        assert a["confianca"] != "confirmado" or a["taxa_casamento"] is not None


def test_duvida_nao_muda_arestas(tmp_path: Path) -> None:
    doc = preparar_resolver(tmp_path)
    antes = por_id(doc)
    escrever_resolvido(doc, "p2-001", [{
        "coluna": COL2, "resultado": "duvida", "justificativa": "dois pais plausíveis",
        "verificacao": "ver-fato2.sql",
    }])
    rodar("resolver", "--doc", str(doc))
    g = por_id(doc)
    assert g[A2]["nota"] == antes[A2]["nota"] and g[A2]["confianca"] == antes[A2]["confianca"]
    assert g[A2]["evidencia"][-1] == {
        "tipo": "analise", "resultado": "duvida", "lote": "p2-001", "justificativa": "dois pais plausíveis",
        "verificacao": "ver-fato2.sql",
    }
    assert grafo_de(doc)["resumo"]["analise"]["duvidas"] == 1


def test_sem_evidencia_marca_p3_sem_mudar_nota(tmp_path: Path) -> None:
    doc = preparar_resolver(tmp_path)
    rodar("resolver", "--doc", str(doc))
    a = por_id(doc)["nome:s.fato3.id_x->s.alvo_a"]
    assert a["nota"].startswith("ambíguo") and a["confianca"] == "hipotese"
    assert a["evidencia"][-1] == {"tipo": "analise", "resultado": "sem_evidencia"}
    assert grafo_de(doc)["resumo"]["analise"]["sem_evidencia"] == 1


def test_itens_invalidos_sao_rejeitados_e_contados(tmp_path: Path) -> None:
    doc = preparar_resolver(tmp_path)
    escrever_resolvido(doc, "p1-001", [
        {"coluna": "s.outra.id_y", "resultado": "duvida", "justificativa": "fora do lote"},
        {"coluna": COL1, "resultado": "escolhido", "justificativa": "x", "destinos": [destino("nome:s.fato1.id_x->s.inexistente", "escolhido")]},
        {"coluna": COL1, "resultado": "escolhido", "justificativa": "dois", "destinos": [destino(A1, "escolhido"), destino(B1, "escolhido")]},
        {"coluna": COL1, "resultado": "talvez", "justificativa": "esquema inválido"},
        {"coluna": COL1, "resultado": "duvida", "justificativa": "x" * 301},
    ])
    antes = grafo_de(doc)["arestas"]
    assert rodar("resolver", "--doc", str(doc)) == 0
    depois = grafo_de(doc)
    assert depois["resumo"]["analise"]["itens_rejeitados"] == 5
    assert depois["resumo"]["analise"]["escolhidas"] == 0
    p1 = [a for a in depois["arestas"] if a["origem"]["relacao"] == "fato1"]
    assert p1 == [a for a in antes if a["origem"]["relacao"] == "fato1"]


def test_arquivo_invalido_e_rejeitado(tmp_path: Path) -> None:
    doc = preparar_resolver(tmp_path)
    arquivo = escrever_resolvido(doc, "p1-001", [])
    arquivo.write_text('{"formato": "outro"}', encoding="utf-8")
    (doc / "_controle" / "lotes" / "relacionamentos-p2-001.resolvido.json").write_text("{quebrado", encoding="utf-8")
    assert rodar("resolver", "--doc", str(doc)) == 0
    assert grafo_de(doc)["resumo"]["analise"]["itens_rejeitados"] == 2
    assert grafo_de(doc)["resumo"]["analise"]["lotes"]["resolvidos"] == 0


def test_nao_age_sobre_fk_declarada(tmp_path: Path) -> None:
    fk = aresta("s.fato1.id_x", "s.alvo_a", fonte="fk_declarada", confianca="declarado", nota=None)
    fk["id"] = "fk:s.fato1.fk_x"
    fk["evidencia"] = [{"tipo": "fk_declarada", "restricao": "fk_x", "validada": True, "acao_delete": None, "acao_update": None}]
    base = arestas_base() + [fk]
    doc = montar_doc(tmp_path, base)
    rodar("lotes", "--doc", str(doc))
    lote = ler(doc, "_controle", "lotes", "relacionamentos-p1-001.json")
    assert "fk:s.fato1.fk_x" not in {d["aresta"] for c in lote["colunas"] for d in c["destinos"]}
    escrever_resolvido(doc, "p1-001", [{
        "coluna": COL1, "resultado": "escolhido", "justificativa": "x", "destinos": [destino("fk:s.fato1.fk_x", "preterido"), destino(A1, "escolhido")],
    }])
    rodar("resolver", "--doc", str(doc))
    assert por_id(doc)["fk:s.fato1.fk_x"]["confianca"] == "declarado"
    assert grafo_de(doc)["resumo"]["analise"]["itens_rejeitados"] == 1


def test_resolver_idempotente_e_deterministico(tmp_path: Path) -> None:
    doc = preparar_resolver(tmp_path)
    escrever_resolvido(doc, "p1-001", [{
        "coluna": COL1, "resultado": "escolhido", "justificativa": "j",
        "destinos": [destino(A1, "escolhido"), destino(B1, "preterido"), destino(C1, "descartado")],
    }])
    escrever_resolvido(doc, "p2-001", [{
        "coluna": COL2, "resultado": "escolhido", "justificativa": "j",
        "destinos": [destino(A2, "escolhido"), destino(B2, "descartado")],
    }])
    caminho = doc / "relacionamentos" / "grafo.json"
    rodar("resolver", "--doc", str(doc))
    primeiro = caminho.read_bytes()
    indice = (doc / "_controle" / "lotes" / "relacionamentos-indice.json").read_bytes()
    rodar("resolver", "--doc", str(doc))
    assert caminho.read_bytes() == primeiro
    assert (doc / "_controle" / "lotes" / "relacionamentos-indice.json").read_bytes() == indice


def test_mudar_a_decisao_desfaz_a_anterior(tmp_path: Path) -> None:
    doc = preparar_resolver(tmp_path)
    escrever_resolvido(doc, "p1-001", [{
        "coluna": COL1, "resultado": "escolhido", "justificativa": "j", "destinos": [destino(A1, "escolhido")],
    }])
    rodar("resolver", "--doc", str(doc))
    escrever_resolvido(doc, "p1-001", [{
        "coluna": COL1, "resultado": "escolhido", "justificativa": "j", "destinos": [destino(B1, "escolhido")],
    }])
    rodar("resolver", "--doc", str(doc))
    g = por_id(doc)
    assert g[B1]["nota"] is None and g[A1]["nota"] == f"preterido pelo analista: escolhido {B1}"
    assert g[A1]["confianca"] == "hipotese"
    (doc / "_controle" / "lotes" / "relacionamentos-p1-001.resolvido.json").unlink()
    rodar("resolver", "--doc", str(doc))
    assert por_id(doc)[A1]["confianca"] == "confirmado"  # confiança e nota anteriores restauradas
    assert por_id(doc)[A1]["nota"] == "ambíguo: 2 destinos possíveis"


# ----------------------------------------------------------------- outros comandos


def resolvido_basico(doc: Path) -> None:
    escrever_resolvido(doc, "p1-001", [{
        "coluna": COL1, "resultado": "escolhido", "justificativa": "j",
        "destinos": [destino(A1, "escolhido"), destino(B1, "preterido")],
    }])


def test_outros_comandos_removem_bloco_e_avisam(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    doc = preparar_resolver(tmp_path)
    resolvido_basico(doc)
    rodar("resolver", "--doc", str(doc))
    aplicado = grafo_de(doc)
    assert "analise" in aplicado["resumo"]
    capsys.readouterr()
    assert rodar("fks", "--doc", str(doc)) == 0
    assert "reexecute relacionamentos resolver" in capsys.readouterr().err
    assert "analise" not in grafo_de(doc)["resumo"]
    rodar("resolver", "--doc", str(doc))  # reaplica sem quebrar, com o mesmo resultado
    assert grafo_de(doc) == aplicado


def test_verificar_aplicar_remove_bloco_e_restaura_base(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    doc = preparar_resolver(tmp_path)
    resolvido_basico(doc)
    rodar("resolver", "--doc", str(doc))
    preterida = por_id(doc)[B1]
    base = restaurar_base(preterida)
    assert base["nota"] == "ambíguo: 2 destinos possíveis"
    assert all(e["tipo"] != "analise" for e in base["evidencia"])
    capsys.readouterr()
    assert rodar("verificar", "--aplicar", "--doc", str(doc)) == 0
    assert "reexecute relacionamentos resolver" in capsys.readouterr().err
    assert "analise" not in grafo_de(doc)["resumo"]
    assert all(e["tipo"] != "analise" for a in grafo_de(doc)["arestas"] for e in a["evidencia"])
    assert por_id(doc)[B1]["nota"].startswith("ambíguo")


def test_esquema_do_resolvido_e_valido() -> None:
    from pg_atlas.relacionamentos_resolver import carregar_esquema_resolvido
    import jsonschema

    jsonschema.Draft202012Validator.check_schema(carregar_esquema_resolvido())
