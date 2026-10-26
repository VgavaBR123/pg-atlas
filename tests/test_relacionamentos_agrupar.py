"""Testes offline da tarefa 2.7: `relacionamentos agrupar` e `relatorio relacionamentos` (D-022). Dados fictícios."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from pg_atlas import cli
from pg_atlas import relacionamentos_agrupar as ag
from pg_atlas import relacionamentos_readme as rd
from pg_atlas import relatorio_relacionamentos as rr
from pg_atlas.esqueleto_marcadores import ErroMarcadores, interpretar
from pg_atlas.json_util import escrever_json
from pg_atlas.relacionamentos import ErroRelacionamentos, extrair_fks, montar_grafo, validar


def _rel(nome: str, fks: list[tuple[str, str, str]] | None = None, pai: str | None = None) -> dict[str, Any]:
    restricoes: list[dict[str, Any]] = [{"nome": f"pk_{nome}", "tipo": "pk", "colunas": ["id"]}]
    for ref_schema, ref_rel, col in fks or []:
        restricoes.append({
            "nome": f"fk_{nome}_{ref_rel}", "tipo": "fk", "colunas": [col], "ref_schema": ref_schema,
            "ref_relacao": ref_rel, "ref_colunas": ["id"], "validada": True,
        })
    return {
        "nome": nome, "tipo": "particionada" if nome == "p" else "tabela", "acesso": "ok",
        "particao": {"pai_schema": "s1", "pai_nome": pai} if pai else None, "colunas": [], "restricoes": restricoes,
    }


def _triagem(schema: str, nome: str, classe: str, pai: str | None = None) -> dict[str, Any]:
    return {
        "acesso": "ok", "classe": classe, "linhas_estimadas": 1, "nome": nome, "schema": schema, "sinais": [],
        "particao_de": pai, "tipo": "particionada" if nome == "p" else "tabela",
    }


S1 = [
    _rel("a"), _rel("b", [("s1", "a", "id_a")]), _rel("c", [("s1", "a", "id_a")]), _rel("d", [("s1", "c", "id_c")]),
    _rel("e"), _rel("v1", [("s1", "a", "id_a")]), _rel("v2"), _rel("tmp_x"), _rel("p"),
    _rel("p_1", [("s1", "a", "id_a")], pai="p"),
]
S2 = [_rel("x", [("s1", "a", "id_a")]), _rel("y", [("s2", "x", "id_x")])]
CLASSES = {
    ("s1", "a"): "comum", ("s1", "b"): "comum", ("s1", "c"): "fato_grande", ("s1", "d"): "comum",
    ("s1", "e"): "dominio_candidata", ("s1", "v1"): "vazia", ("s1", "v2"): "vazia",
    ("s1", "tmp_x"): "temporaria_ou_copia", ("s1", "p"): "fato_grande", ("s1", "p_1"): "particao",
    ("s2", "x"): "comum", ("s2", "y"): "comum",
}
PLACEHOLDER = '## {t}\n\n<!-- pg-atlas:pendente fase="2" -->\n_Pendente: a preencher na fase 2._'
README = (
    '<!-- pg-atlas:auto inicio secao="cabecalho" -->\n# Schema {s}\n<!-- pg-atlas:auto fim -->\n\n'
    "## Do que trata\n\nTexto escrito à mão.\n\n" + PLACEHOLDER.format(t="Grupos de tabelas")
    + "\n\n## Inventário\n\nx\n\n" + PLACEHOLDER.format(t="Ligações com outros schemas") + "\n"
)


def _json(caminho: Path, dados: Any) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")


def _montar(tmp_path: Path, extras: list[dict[str, Any]] | None = None) -> Path:
    doc = tmp_path / "doc"
    (doc / "config").mkdir(parents=True)
    (doc / "config" / "projeto.yml").write_text('projeto:\n  nome: "t"\n', encoding="utf-8")
    estruturas = {"s1": S1, "s2": S2}
    for s, relacoes in estruturas.items():
        _json(doc / "catalogo" / "estrutura" / f"{s}.json", {"schema": s, "relacoes": relacoes, "funcoes": []})
        (doc / "schemas" / s).mkdir(parents=True)
        (doc / "schemas" / s / "README.md").write_text(README.format(s=s), encoding="utf-8")
    pais = {("s1", "p_1"): "s1.p"}
    triagem = [_triagem(s, n, c, pais.get((s, n))) for (s, n), c in sorted(CLASSES.items())]
    _json(doc / "catalogo" / "_triagem.json", {"tabelas": triagem})
    _json(doc / "catalogo" / "_banco.json", {"extraido_em": "2026-01-01T07:00:00+00:00"})
    estr = {s: {"relacoes": r} for s, r in estruturas.items()}
    fks, _ = extrair_fks(estr)
    grafo = montar_grafo(fks + (extras or []), [])
    validar(grafo)
    escrever_json(doc / "relacionamentos" / "grafo.json", grafo)
    return doc


def _inferida(origem: str, destino: str, confianca: str, taxa: float | None) -> dict[str, Any]:
    so, ro = origem.split(".")
    sd, rd_ = destino.split(".")
    return {
        "id": f"nome:{origem}.id_x->{destino}.id", "origem": {"schema": so, "relacao": ro, "colunas": ["id_x"]},
        "destino": {"schema": sd, "relacao": rd_, "colunas": ["id"]}, "confianca": confianca,
        "fonte": "inferencia_nome", "taxa_casamento": taxa, "nota": None,
        "evidencia": [{"tipo": "inferencia_nome", "regra": "exato", "pontuacao": 1.0, "candidatos": 1}],
    }


def _rodar(*args: str) -> int:
    return cli.main(["relacionamentos", "agrupar", *args])


def _lotes(doc: Path) -> dict[str, Any]:
    return json.loads((doc / "relacionamentos" / "lotes.json").read_text(encoding="utf-8"))


# ----------------------------------------------------------------- algoritmo


def test_nos_e_particao_acompanha_o_pai() -> None:
    triagem = [_triagem(s, n, c, "s1.p" if n == "p_1" else None) for (s, n), c in CLASSES.items()]
    no_de, membros = ag.mapear_nos(triagem)
    assert no_de[("s1", "p_1")] == ("s1", "p")
    assert membros[("s1", "p")] == [("s1", "p"), ("s1", "p_1")]
    assert ("s1", "p_1") not in membros


def test_propagacao_respeita_o_limite_e_e_deterministica() -> None:
    nos = [("s", f"t{i:02d}") for i in range(30)]
    adj: ag.Adjacencia = {n: {} for n in nos}
    for a, b in zip(nos, nos[1:]):  # corrente de 30 tabelas
        adj[a][b] = adj[b][a] = 2
    tamanho = {n: 1 for n in nos}
    r1 = ag.propagar_rotulos(adj, tamanho, 8)
    assert r1 == ag.propagar_rotulos(adj, tamanho, 8)
    assert max(Counter(r1.values()).values()) <= 8
    lotes = ag.agrupar_nos(adj, tamanho, 8)
    assert sorted(n for lote in lotes for c in lote for n in c) == nos
    assert all(sum(len(c) for c in lote) <= 8 for lote in lotes)


def test_componentes_conexos() -> None:
    adj: ag.Adjacencia = {("s", "a"): {("s", "b"): 1}, ("s", "b"): {("s", "a"): 1}, ("s", "c"): {}}
    assert ag.componentes_conexos(adj) == [[("s", "a"), ("s", "b")], [("s", "c")]]


def test_lotes_leves_cortam_em_ordem() -> None:
    nos = [("s", f"t{i}") for i in range(5)]
    assert ag.lotes_leves(nos, {n: 1 for n in nos}, 2) == [nos[:2], nos[2:4], nos[4:]]


# ----------------------------------------------------------------- comando


def test_agrupar_gera_lotes_validos(tmp_path: Path) -> None:
    doc = _montar(tmp_path)
    assert _rodar("--doc", str(doc)) == 0
    lotes = _lotes(doc)
    ag.validar(lotes)
    todas = [f"{t['schema']}.{t['nome']}" for lote in lotes["lotes"] for t in lote["tabelas"]]
    assert sorted(todas) == sorted(f"{s}.{n}" for s, n in CLASSES)  # cada tabela em exatamente um lote
    grafo = [lote for lote in lotes["lotes"] if lote["tipo"] == "grafo"]
    leves = [lote for lote in lotes["lotes"] if lote["tipo"] == "leve"]
    assert {c for lote in grafo for c in lote["por_classe"]} <= {"comum", "fato_grande", "dominio_candidata", "particao"}
    assert {lote["classe_leve"] for lote in leves} == {"vazia", "temporaria_ou_copia"}
    assert {t["nome"] for lote in leves for t in lote["tabelas"]} == {"v1", "v2", "tmp_x"}
    lote_p = next(lote for lote in grafo if any(t["nome"] == "p" for t in lote["tabelas"]))
    assert {t["nome"] for t in lote_p["tabelas"]} >= {"p", "p_1"}
    r = lotes["resumo"]
    assert r["tabelas_total"] == len(CLASSES) and r["total_lotes"] == len(lotes["lotes"])
    assert r["arestas"]["com_tabela_leve"] == 1  # v1 -> a


def test_hipotese_fica_fora_por_padrao(tmp_path: Path) -> None:
    doc = _montar(tmp_path, [_inferida("s1.e", "s1.d", "hipotese", None)])
    _rodar("--doc", str(doc))
    assert _lotes(doc)["parametros"]["confiancas_usadas"] == ["confirmado", "declarado"]
    usadas = _lotes(doc)["resumo"]["arestas"]["usadas"]
    _rodar("--doc", str(doc), "--incluir-hipotese")
    assert _lotes(doc)["resumo"]["arestas"]["usadas"] == usadas + 1
    assert _lotes(doc)["parametros"]["confiancas_usadas"] == ["confirmado", "declarado", "hipotese"]


def test_confirmado_sem_taxa_falha_sem_gravar(tmp_path: Path) -> None:
    doc = _montar(tmp_path, [_inferida("s1.e", "s1.d", "confirmado", None)])
    with pytest.raises(ErroRelacionamentos, match="taxa"):
        ag.agrupar(doc)
    assert not (doc / "relacionamentos" / "lotes.json").exists()


def test_fk_fora_do_grafo_falha(tmp_path: Path) -> None:
    doc = _montar(tmp_path)
    caminho = doc / "relacionamentos" / "grafo.json"
    arestas = json.loads(caminho.read_text(encoding="utf-8"))["arestas"]
    escrever_json(caminho, montar_grafo(arestas[1:], []))
    with pytest.raises(ErroRelacionamentos, match="FK declarada"):
        ag.agrupar(doc)


def test_exige_grafo(tmp_path: Path) -> None:
    doc = _montar(tmp_path)
    (doc / "relacionamentos" / "grafo.json").unlink()
    with pytest.raises(ErroRelacionamentos, match="Grafo ausente"):
        ag.agrupar(doc)


def test_saida_deterministica(tmp_path: Path) -> None:
    doc = _montar(tmp_path)
    _rodar("--doc", str(doc))
    arquivos = [doc / "relacionamentos" / "lotes.json", *sorted((doc / "schemas").glob("*/README.md"))]
    antes = [a.read_bytes() for a in arquivos]
    _rodar("--doc", str(doc))
    assert [a.read_bytes() for a in arquivos] == antes
    assert b"\r" not in antes[0] and antes[0].endswith(b"\n")


def test_tamanho_maximo_invalido(tmp_path: Path) -> None:
    doc = _montar(tmp_path)
    with pytest.raises(ErroRelacionamentos):
        ag.agrupar(doc, max_tabelas=1)


# ----------------------------------------------------------------- README


def test_readme_ocupa_a_pendencia_e_preserva_o_resto(tmp_path: Path) -> None:
    doc = _montar(tmp_path)
    _rodar("--doc", str(doc))
    t = (doc / "schemas" / "s1" / "README.md").read_text(encoding="utf-8")
    assert list(interpretar(t.split("\n"))) == ["cabecalho", "grupos", "ligacoes"]
    assert "Texto escrito à mão." in t and "## Inventário\n\nx" in t
    assert "pg-atlas:pendente" not in t
    assert "```mermaid\nflowchart LR" in t
    assert "| `s2` |" in t  # ligação com o outro schema


def test_readme_sem_marcadores_vai_para_o_fim_e_marcador_quebrado_nao_grava(tmp_path: Path) -> None:
    texto = "# Schema\n\nTexto.\n"
    novo = rd.aplicar_secao(texto, "grupos", "## Grupos de tabelas\n\nx", "Grupos de tabelas")
    assert novo.startswith(texto.rstrip("\n")) and novo.endswith("<!-- pg-atlas:auto fim -->\n")
    assert rd.aplicar_secao(novo, "grupos", "## Grupos de tabelas\n\ny", "Grupos de tabelas").count("y") == 1
    doc = _montar(tmp_path)
    quebrado = '<!-- pg-atlas:auto inicio secao="grupos" -->\nx\n'
    (doc / "schemas" / "s2" / "README.md").write_text(quebrado, encoding="utf-8")
    antes = (doc / "schemas" / "s1" / "README.md").read_bytes()
    assert _rodar("--doc", str(doc)) == ErroMarcadores.codigo_saida
    assert (doc / "schemas" / "s1" / "README.md").read_bytes() == antes  # nada gravado


def test_readme_ausente_falha(tmp_path: Path) -> None:
    doc = _montar(tmp_path)
    (doc / "schemas" / "s2" / "README.md").unlink()
    assert _rodar("--doc", str(doc)) == ErroRelacionamentos.codigo_saida


def test_diagrama_limita_nos_e_arestas() -> None:
    tabelas = [("s", f"t{i:02d}") for i in range(40)]
    pares: rd.Pares = {(tabelas[0], t): "declarado" for t in tabelas[1:]}
    pares[(tabelas[1], tabelas[2])] = "confirmado"
    desenho = rd.diagrama(tabelas, pares)
    assert desenho is not None
    texto, desenhados, total = desenho
    assert desenhados == rd.MAX_NOS and total == 40
    assert texto.count("-->") + texto.count("-.->") <= rd.MAX_ARESTAS
    assert rd.diagrama(tabelas, {}) is None
    a, b = ("s", 'a"[mascarado:cpf:1]'), ("s", "b")
    mascarada = rd.diagrama([a, b], {(a, b): "declarado"})
    assert mascarada is not None and "#quot;" in mascarada[0]


def test_sem_readme_nao_toca_nos_readmes(tmp_path: Path) -> None:
    doc = _montar(tmp_path)
    antes = (doc / "schemas" / "s1" / "README.md").read_bytes()
    assert _rodar("--doc", str(doc), "--sem-readme") == 0
    assert (doc / "schemas" / "s1" / "README.md").read_bytes() == antes


# ----------------------------------------------------------------- relatório


def _preparar_custos(doc: Path) -> None:
    ctl = doc / "_controle" / "custos"
    ctl.mkdir(parents=True)
    marcas = [
        {"evento": "inicio", "fase": 2, "quando": "2026-01-01T08:00:00-04:00"},
        {"evento": "inicio", "fase": 2, "tarefa": "2.7", "quando": "2026-01-01T08:10:00-04:00"},
        {"evento": "fim", "fase": 2, "tarefa": "2.7", "quando": "2026-01-01T08:40:00-04:00"},
    ]
    (ctl / "fases.jsonl").write_text("".join(json.dumps(m) + "\n" for m in marcas), encoding="utf-8")
    modelo = {"entrada": 1, "saida": 2, "cache_escrita_5m": 0, "cache_escrita_1h": 3, "cache_leitura": 4, "respostas": 1}
    consumo = {"periodo": {"fim": "2026-01-02T00:00:00Z"}, "fases": {"2": {"horas": 0.5, "modelos": {"m": modelo}}}}
    _json(ctl / "consumo.json", consumo)
    pend = doc / "_controle" / "verificacoes" / "pendentes"
    pend.mkdir(parents=True)
    (pend / "a.sql").write_text("select 1", encoding="utf-8")
    (pend / "b.sql").write_text("select 2", encoding="utf-8")


def test_relatorio_cria_preserva_observacoes_e_e_idempotente(tmp_path: Path) -> None:
    doc = _montar(tmp_path, [_inferida("s1.e", "s1.d", "confirmado", 0.97)])
    _preparar_custos(doc)
    _rodar("--doc", str(doc))
    assert rr.gerar_relatorio(doc, "Teste") == "criado"
    destino = doc / rr.DESTINO
    t = destino.read_text(encoding="utf-8")
    assert list(interpretar(t.split("\n"))) == list(rr.SECOES)
    assert "# Relatório da fase 2: relacionamentos" in t and "| 2 |" in t
    assert "Resultado geral: OK." in t and "Tarefa 2.7" in t and "Tempo da extração" not in t
    assert "| inferencia_nome | 0 | 1 | 0 | 1 |" in t
    assert t.rstrip("\n").endswith("nunca reescreve esta seção._") and "\r" not in t
    destino.write_text(t + "\nNota minha.\n", encoding="utf-8")
    assert rr.gerar_relatorio(doc, "Teste") == "inalterado"
    assert "Nota minha." in destino.read_text(encoding="utf-8")


def test_relatorio_pela_cli_exige_lotes(tmp_path: Path) -> None:
    doc = _montar(tmp_path)
    _preparar_custos(doc)
    assert cli.main(["relatorio", "relacionamentos", "--doc", str(doc)]) != 0  # sem lotes.json
    _rodar("--doc", str(doc))
    assert cli.main(["relatorio", "relacionamentos", "--doc", str(doc)]) == 0
    assert (doc / rr.DESTINO).is_file()


def test_distribuicao_de_tamanhos() -> None:
    lotes = [{"tipo": "grafo", "total_tabelas": n} for n in (1, 3, 4, 20)] + [{"tipo": "leve", "total_tabelas": 60}]
    assert rr.distribuicao(lotes, "grafo") == [("1", 1), ("2 a 5", 2), ("16 a 20", 1)]
    assert rr.distribuicao(lotes, "leve") == [("41 ou mais", 1)]
