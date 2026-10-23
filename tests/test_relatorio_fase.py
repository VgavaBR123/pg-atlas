"""Testes offline do comando `relatorio catalogo` (tarefa 1.6, D-015). Dados fictícios."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pg_atlas import cli
from pg_atlas import relatorio_fase as rf
from pg_atlas.esqueleto_marcadores import ErroMarcadores, interpretar

RAIZ = Path(__file__).resolve().parent.parent


def _json(caminho: Path, dados: Any) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")


def _rel(nome: str, tipo: str = "tabela", acesso: str = "ok", pai: str | None = None) -> dict[str, Any]:
    particao = {"pai_schema": "s1", "pai_nome": pai} if pai else None
    return {"nome": nome, "tipo": tipo, "acesso": acesso, "gatilhos": [], "particao": particao}


def _est(nome: str, linhas: int, tamanho: int, tipo: str = "tabela") -> dict[str, Any]:
    return {"nome": nome, "tipo": tipo, "linhas_estimadas": linhas, "tamanho_total_bytes": tamanho}


MARCAS = [
    {"evento": "inicio", "fase": 1, "quando": "2026-01-01T08:00:00-04:00"},
    {"evento": "inicio", "fase": 1, "tarefa": "1.1", "quando": "2026-01-01T08:10:00-04:00"},
    {"evento": "fim", "fase": 1, "tarefa": "1.1", "quando": "2026-01-01T08:40:00-04:00"},
    {"evento": "fim", "fase": 1, "tarefa": "1.1", "quando": "2026-01-01T08:50:00-04:00"},
    {"evento": "inicio", "fase": 1, "tarefa": "1.2", "quando": "2026-01-01T09:00:00-04:00"},
]

TOTAIS = {
    "tabela": 6, "particionada": 1, "view": 1, "materializada": 1, "estrangeira": 0,
    "funcoes": 4, "gatilhos": 3, "sequencias": 2, "indices": 9, "restricoes": 8,
    "colunas": 40, "tipos": 1, "sem_select": 2,
}  # fmt: skip


@pytest.fixture
def doc(tmp_path: Path) -> Path:
    d = tmp_path / "doc"
    (d / "config").mkdir(parents=True)
    (d / "config" / "projeto.yml").write_text('projeto:\n  nome: "Banco de teste"\n', encoding="utf-8")
    cat, ctl = d / "catalogo", d / "_controle"
    _json(cat / "_banco.json", {
        "extraido_em": "2026-01-01T07:00:00+00:00", "formato": "banco", "tamanho_bytes": 5 * 1024**3,
        "total_schemas": 2, "versao": "PostgreSQL 15.5 (Debian) on x86_64",
        "schemas": {"s1": {}, "s2": {}}, "totais": TOTAIS,
    })  # fmt: skip
    _json(cat / "estrutura" / "s1.json", {
        "schema": "s1",
        "relacoes": [
            _rel("grande"), _rel("pequena"), _rel("hist", "particionada"),
            _rel("hist_a", pai="hist"), _rel("hist_b", pai="hist"), _rel("visao", "view", "sem_select"),
        ],
        "funcoes": [{"nome": "f1", "tipo": "function", "acesso": "ok"},
                    {"nome": "p1", "tipo": "procedure", "acesso": "sem_execute"}],
    })  # fmt: skip
    _json(cat / "estrutura" / "s2.json", {
        "schema": "s2",
        "relacoes": [_rel("t2", acesso="sem_select"), _rel("mv", "materializada")],
        "funcoes": [{"nome": "f2", "tipo": "function", "acesso": "ok"},
                    {"nome": "ag", "tipo": "aggregate", "acesso": "ok"}],
    })  # fmt: skip
    _json(cat / "estatisticas" / "s1.json", {"schema": "s1", "relacoes": [
        _est("grande", 123456, 3000), _est("pequena", 5, 100), _est("hist", -1, 0, "particionada"),
        _est("hist_a", 1000, 2000), _est("hist_b", 2000, 2500), _est("visao", 0, 0, "view"),
    ]})  # fmt: skip
    _json(cat / "estatisticas" / "s2.json", {"schema": "s2", "relacoes": [
        _est("t2", 10, 4000), _est("mv", 50, 9000, "materializada"),
    ]})  # fmt: skip
    _json(cat / "_triagem.json", {
        "criterios": {"precedencia": ["particao", "vazia", "comum"]},
        "resumo": {"por_classe": {"comum": 3, "vazia": 1, "particao": 2}, "total": 6},
    })  # fmt: skip
    _json(ctl / "verificacao-catalogo.json", {
        "ok": True, "banco_consultado": True, "verificado_em": "2026-01-01T07:30:00+00:00",
        "verificacoes": [{"nome": "contagem", "ok": True, "resumo": {"schemas_comparados": 2}}],
    })  # fmt: skip
    _json(ctl / "mascaramento.json", {"registros": [
        {"tipo": "cpf", "quantidade": 3, "objeto": "x", "schema": "s1", "campo": "codigo"},
        {"tipo": "cpf", "quantidade": 2, "objeto": "y", "schema": "s1", "campo": "codigo"},
        {"tipo": "email", "quantidade": 1, "objeto": "x", "schema": "s1", "campo": "codigo"},
    ]})  # fmt: skip
    (ctl / "custos").mkdir(parents=True)
    (ctl / "custos" / "fases.jsonl").write_text("".join(json.dumps(m) + "\n" for m in MARCAS), encoding="utf-8")
    modelo = {"entrada": 10, "saida": 20, "cache_escrita_5m": 0, "cache_escrita_1h": 30,
              "cache_leitura": 4000, "respostas": 3}  # fmt: skip
    _json(ctl / "custos" / "consumo.json", {
        "periodo": {"inicio": "2026-01-01T00:00:00Z", "fim": "2026-01-02T00:00:00Z"},
        "fases": {"1": {"horas": 1.5, "modelos": {"modelo-x": modelo},
                        "tarefas": {"1.1": {"horas": 0.5, "modelos": {}}}}},
    })  # fmt: skip
    return d


def texto(doc: Path) -> str:
    return (doc / rf.DESTINO).read_text(encoding="utf-8")


def test_cria_relatorio_com_todas_as_secoes(doc: Path) -> None:
    assert rf.gerar_relatorio(doc, "Banco de teste") == "criado"
    t = texto(doc)
    assert list(interpretar(t.split("\n"))) == list(rf.SECOES)
    assert t.rstrip("\n").endswith("nunca reescreve esta seção._")
    assert "\r" not in t and t.endswith("\n")
    assert "**Projeto:** Banco de teste" in t
    assert "PostgreSQL 15.5" in t and "Debian" not in t
    assert "| Procedimentos (procedures) | 1 |" in t
    assert "| Agregações e outras rotinas | 1 |" in t
    assert "(inclui 2 partições)" in t


def test_triagem_na_ordem_de_precedencia(doc: Path) -> None:
    rf.gerar_relatorio(doc, "x")
    t = texto(doc)
    assert t.index("| particao | 2 |") < t.index("| vazia | 1 |") < t.index("| comum | 3 |")
    assert "| **Total** | 6 | 100,0% |" in t


def test_particionada_soma_particoes_e_particoes_fora_do_ranking(doc: Path) -> None:
    itens = rf.ranking_maiores(rf.carregar_entradas(doc, "x"))
    nomes = [(i["schema"], i["nome"]) for i in itens]
    assert ("s1", "hist_a") not in nomes and ("s1", "hist_b") not in nomes
    assert "visao" not in [n for _, n in nomes]  # view não entra
    assert nomes == [("s2", "mv"), ("s1", "hist"), ("s2", "t2"), ("s1", "grande"), ("s1", "pequena")]
    hist = next(i for i in itens if i["nome"] == "hist")
    assert hist["tamanho"] == 4500 and hist["linhas"] == 3000


def test_ranking_limitado_a_20_com_desempate_por_nome(doc: Path) -> None:
    rels = [_rel(f"t{n:02d}") for n in range(25)]
    _json(doc / "catalogo" / "estrutura" / "s2.json", {"schema": "s2", "relacoes": rels, "funcoes": []})
    _json(doc / "catalogo" / "estatisticas" / "s2.json",
          {"schema": "s2", "relacoes": [_est(r["nome"], 1, 777) for r in rels]})  # fmt: skip
    itens = rf.ranking_maiores(rf.carregar_entradas(doc, "x"))
    assert len(itens) == rf.TOP_MAIORES
    # empate de tamanho: ordem por (schema, nome); s1.grande (3000) vem antes dos empatados
    assert [(i["schema"], i["nome"]) for i in itens[:3]] == [("s1", "hist"), ("s1", "grande"), ("s2", "t00")]


def test_sem_acesso_so_contagens(doc: Path) -> None:
    rf.gerar_relatorio(doc, "x")
    t = texto(doc)
    assert "| s1 | view | 1 |" in t and "| s2 | tabela | 1 |" in t
    assert "| **Total** |  | 2 |" in t
    assert "| s1 | procedure | 1 |" in t
    assert "catalogo/_triagem.json" in t
    bloco = t.split("## Objetos sem acesso")[1].split("## Mascaramento")[0]
    assert "t2" not in bloco and "visao" not in bloco  # sem lista nominal


def test_mascaramento_totais_por_tipo(doc: Path) -> None:
    rf.gerar_relatorio(doc, "x")
    t = texto(doc)
    assert "| cpf | 5 |" in t and "| email | 1 |" in t and "| **Total** | 6 |" in t


def test_tempo_fase_tarefa_e_em_andamento(doc: Path) -> None:
    rf.gerar_relatorio(doc, "x")
    t = texto(doc)
    # fase sem fim: em andamento até a última marca; tarefa 1.1 usa o último fim (08:50)
    assert "| Fase 1 | 2026-01-01 08:00 | em andamento até 2026-01-01 09:00 | 1 h 00 min | 1,50 |" in t
    assert "| Tarefa 1.1 | 2026-01-01 08:10 | 2026-01-01 08:50 | 40 min | 0,50 |" in t
    assert "| Tarefa 1.2 | 2026-01-01 09:00 | em andamento até 2026-01-01 09:00 | 0 min | — |" in t
    assert "não registrado" in t


def test_custo_sem_preco_informa_preco_nao_informado(doc: Path) -> None:
    rf.gerar_relatorio(doc, "x")
    t = texto(doc)
    assert "| modelo-x | 10 | 20 | 30 | 4.000 | preço não informado |" in t
    assert "AVISO: preço não informado" in t and "AVISO: câmbio não informado" in t


def test_custo_com_preco(doc: Path) -> None:
    (doc / "config" / "precos.yml").write_text(
        "modelos:\n  modelo-x:\n    entrada: 1000000\n    saida: 0\n    cache_escrita_1h: 0\n"
        "    cache_leitura: 0\n    fonte: teste\n    consultado_em: 2026-01-01\n",
        encoding="utf-8",
    )
    rf.gerar_relatorio(doc, "x")
    assert "US$ 10,00" in texto(doc)


def test_regerar_preserva_observacoes_e_texto_fora_dos_marcadores(doc: Path) -> None:
    rf.gerar_relatorio(doc, "x")
    escrito = (
        texto(doc).replace(rf.TEXTO_OBSERVACOES, "## Observações\n\nTexto humano único, com acentuação.\n\n- item\n")
        + "\nRodapé humano.\n"
    )
    (doc / rf.DESTINO).write_text(escrito, encoding="utf-8")
    _json(doc / "catalogo" / "_triagem.json", {
        "resumo": {"por_classe": {"comum": 9}, "total": 9}, "criterios": {"precedencia": ["comum"]},
    })  # fmt: skip
    assert rf.gerar_relatorio(doc, "x") == "atualizado"
    novo = texto(doc)
    assert "Texto humano único, com acentuação." in novo and "- item" in novo and "Rodapé humano." in novo
    assert "| comum | 9 |" in novo and "| vazia |" not in novo


def test_determinismo_bytes_identicos(doc: Path) -> None:
    rf.gerar_relatorio(doc, "x")
    primeiro = (doc / rf.DESTINO).read_bytes()
    assert rf.gerar_relatorio(doc, "x") == "inalterado"
    assert (doc / rf.DESTINO).read_bytes() == primeiro
    (doc / rf.DESTINO).unlink()
    rf.gerar_relatorio(doc, "x")
    assert (doc / rf.DESTINO).read_bytes() == primeiro


def test_marcadores_quebrados_nao_sao_reescritos(doc: Path) -> None:
    rf.gerar_relatorio(doc, "x")
    quebrado = texto(doc).replace("<!-- pg-atlas:auto fim -->", "", 1)
    (doc / rf.DESTINO).write_text(quebrado, encoding="utf-8")
    with pytest.raises(ErroMarcadores):
        rf.gerar_relatorio(doc, "x")
    assert texto(doc) == quebrado


@pytest.mark.parametrize(
    "arquivo",
    [
        "catalogo/_banco.json", "catalogo/estrutura/s1.json", "catalogo/estatisticas/s2.json",
        "catalogo/_triagem.json", "_controle/verificacao-catalogo.json", "_controle/mascaramento.json",
        "_controle/custos/fases.jsonl", "_controle/custos/consumo.json",
    ],
)  # fmt: skip
def test_arquivo_faltando_da_erro_claro(doc: Path, arquivo: str) -> None:
    (doc / arquivo).unlink()
    with pytest.raises(rf.ErroRelatorio) as erro:
        rf.gerar_relatorio(doc, "x")
    assert Path(arquivo).name in str(erro.value)
    assert not (doc / rf.DESTINO).exists()


def test_json_invalido_da_erro_claro(doc: Path) -> None:
    (doc / "catalogo" / "_triagem.json").write_text("{nao e json", encoding="utf-8")
    with pytest.raises(rf.ErroRelatorio, match="_triagem.json"):
        rf.gerar_relatorio(doc, "x")


def test_cli_registrada_e_sem_conexao(
    doc: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import pg_atlas.conexao as conexao

    def proibido(*a: Any, **k: Any) -> None:
        raise AssertionError("o relatório não pode conectar ao banco")

    monkeypatch.setattr(conexao.Conexao, "__init__", proibido)
    assert cli.main(["relatorio", "catalogo", "--doc", str(doc)]) == 0
    assert "criado" in capsys.readouterr().out
    assert cli.main(["relatorio", "catalogo", "--doc", str(doc)]) == 0
    assert "inalterado" in capsys.readouterr().out


def test_cli_arquivo_faltando_sai_com_erro(doc: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (doc / "_controle" / "custos" / "consumo.json").unlink()
    assert cli.main(["relatorio", "catalogo", "--doc", str(doc)]) == rf.ErroRelatorio.codigo_saida
    assert "custos coletar" in capsys.readouterr().err


def test_modelo_tem_as_mesmas_secoes_do_codigo() -> None:
    modelo = (RAIZ / "templates" / "relatorio-fase-1.md").read_text(encoding="utf-8")
    assert list(interpretar(modelo.split("\n"))) == list(rf.SECOES)
    assert rf.TEXTO_OBSERVACOES in modelo
