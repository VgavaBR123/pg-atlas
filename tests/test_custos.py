"""Testes offline do coletor de custos, com logs sintéticos."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from pg_atlas.cli import main
from pg_atlas.custos import codificar_pasta_workspace

PROJETO_YML = 'projeto:\n  nome: "Projeto de teste"\ncustos:\n  plano: ""\n  valor_mensal_brl: null\n'
LINHA_MANUAL = '{"evento": "inicio", "fase": 0, "quando": "2026-10-23T07:28:12-04:00"}\n'


@pytest.fixture
def doc(tmp_path: Path) -> Path:
    pasta = tmp_path / "doc"
    (pasta / "config").mkdir(parents=True)
    (pasta / "config" / "projeto.yml").write_text(PROJETO_YML, encoding="utf-8")
    return pasta


def linha(ts: str, msg_id: str | None, modelo: str = "claude-teste", entrada: int = 10, saida: int = 5,
          sessao: str = "s1", tipo: str = "assistant", req: str | None = "req1") -> str:
    msg: dict = {
        "model": modelo,
        "usage": {
            "input_tokens": entrada,
            "output_tokens": saida,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 1000,
            "cache_creation": {"ephemeral_5m_input_tokens": 40, "ephemeral_1h_input_tokens": 60},
        },
    }
    if msg_id:
        msg["id"] = msg_id
    obj = {"type": tipo, "timestamp": ts, "sessionId": sessao, "message": msg}
    if req:
        obj["requestId"] = req
    return json.dumps(obj)


def escrever_logs(pasta: Path, nome: str, linhas: list[str]) -> None:
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / nome).write_text("\n".join(linhas) + "\n", encoding="utf-8")


def coletar(doc: Path, projetos: Path) -> dict:
    assert main(["custos", "coletar", "--doc", str(doc), "--projetos", str(projetos)]) == 0
    return json.loads((doc / "_controle" / "custos" / "consumo.json").read_text(encoding="utf-8"))


def escrever_fases(doc: Path, linhas: list[str]) -> None:
    arquivo = doc / "_controle" / "custos" / "fases.jsonl"
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    arquivo.write_text("".join(l + "\n" for l in linhas), encoding="utf-8")


def test_codificacao_da_pasta() -> None:
    assert codificar_pasta_workspace(Path("C:\\dev\\atlas")) == "C--dev-atlas"


def test_deduplica_por_message_id_e_fica_com_a_ultima(doc: Path, tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    escrever_logs(proj, "s1.jsonl", [
        linha("2026-10-23T12:00:00Z", "m1", saida=1),
        linha("2026-10-23T12:00:01Z", "m1", saida=7),  # mesma resposta, outro bloco
        linha("2026-10-23T12:00:02Z", None, req="rA", saida=3),  # sem id: usa requestId
        linha("2026-10-23T12:00:03Z", None, req="rA", saida=4),
        linha("2026-10-23T12:00:04Z", "m2", tipo="user"),  # outro tipo
        linha("2026-10-23T12:00:05Z", "m3", modelo="<synthetic>"),
        "{lixo",
    ])
    escrever_logs(proj / "s1" / "subagents", "agent-x.jsonl", [linha("2026-10-23T12:01:00Z", "m4", saida=2, req="r4")])
    consumo = coletar(doc, proj)
    t = consumo["modelos"]["claude-teste"]
    assert t["respostas"] == 3
    assert t["saida"] == 7 + 4 + 2
    assert t["entrada"] == 30
    assert t["cache_escrita_5m"] == 120 and t["cache_escrita_1h"] == 180
    assert t["cache_leitura"] == 3000
    assert "<synthetic>" not in consumo["modelos"]
    assert consumo["sessoes"] == 1
    assert consumo["horas_execucao"] == pytest.approx(60 / 3600, abs=1e-4)


def test_atribuicao_a_fase_tarefa_e_sem_fase(doc: Path, tmp_path: Path) -> None:
    escrever_fases(doc, [
        LINHA_MANUAL.strip(),  # 11:28:12Z, fase 0 sem tarefa, sem fim
        '{"evento": "inicio", "fase": 0, "tarefa": "0.8", "quando": "2026-10-23T08:00:00-04:00"}',
        '{"evento": "fim", "fase": 0, "tarefa": "0.8", "quando": "2026-10-23T09:00:00-04:00"}',
    ])
    proj = tmp_path / "proj"
    escrever_logs(proj, "s1.jsonl", [
        linha("2026-10-23T11:00:00Z", "a"),  # antes de tudo: sem_fase
        linha("2026-10-23T11:30:00Z", "b"),  # fase 0
        linha("2026-10-23T12:30:00Z", "c"),  # tarefa 0.8 (mais interna)
        linha("2026-10-23T12:45:00Z", "d"),  # tarefa 0.8
        linha("2026-10-23T13:30:00Z", "e"),  # volta à fase 0
    ])
    consumo = coletar(doc, proj)
    fases = consumo["fases"]
    assert fases["sem_fase"]["modelos"]["claude-teste"]["respostas"] == 1
    assert fases["0"]["modelos"]["claude-teste"]["respostas"] == 4
    assert fases["0"]["tarefas"]["0.8"]["modelos"]["claude-teste"]["respostas"] == 2
    assert fases["0"]["tarefas"]["0.8"]["horas"] == pytest.approx(0.25, abs=1e-4)


def test_marcar_inicio_fim_preserva_linha_manual(doc: Path) -> None:
    arquivo = doc / "_controle" / "custos" / "fases.jsonl"
    escrever_fases(doc, [LINHA_MANUAL.strip()])
    assert main(["custos", "marcar-inicio", "--fase", "0", "--tarefa", "0.8", "--doc", str(doc)]) == 0
    assert main(["custos", "marcar-fim", "--fase", "0", "--doc", str(doc)]) == 0
    linhas = arquivo.read_text(encoding="utf-8").splitlines()
    assert linhas[0] + "\n" == LINHA_MANUAL
    um, dois = json.loads(linhas[1]), json.loads(linhas[2])
    assert list(um) == ["evento", "fase", "tarefa", "quando"] and um["evento"] == "inicio"
    assert "tarefa" not in dois and dois["evento"] == "fim"
    assert len(um["quando"]) == 25  # AAAA-MM-DDTHH:MM:SS+HH:MM


def test_saida_deterministica(doc: Path, tmp_path: Path) -> None:
    escrever_fases(doc, [LINHA_MANUAL.strip()])
    proj = tmp_path / "proj"
    escrever_logs(proj, "b.jsonl", [linha("2026-10-23T12:00:00Z", "m1", sessao="sb"), linha("2026-10-23T12:10:00Z", "m2", modelo="outro", sessao="sb")])
    escrever_logs(proj, "a.jsonl", [linha("2026-10-23T12:05:00Z", "m3", sessao="sa")])
    destino = doc / "_controle" / "custos" / "consumo.json"
    coletar(doc, proj)
    primeiro = destino.read_bytes()
    coletar(doc, proj)
    assert destino.read_bytes() == primeiro
    assert b"\r" not in primeiro and primeiro.endswith(b"\n")
    texto = primeiro.decode("utf-8")
    assert "conversa" not in texto and str(proj) not in texto


def test_relatorio_sem_precos_nem_cambio(doc: Path, tmp_path: Path) -> None:
    escrever_fases(doc, [LINHA_MANUAL.strip()])
    proj = tmp_path / "proj"
    escrever_logs(proj, "s1.jsonl", [linha("2026-10-23T12:00:00Z", "m1")])
    coletar(doc, proj)
    assert main(["custos", "relatorio", "--doc", str(doc)]) == 0
    assert main(["custos", "relatorio", "--parcial", "--fase", "0", "--doc", str(doc)]) == 0
    completo = (doc / "relatorios" / "custo-implementacao.md").read_text(encoding="utf-8")
    parcial = (doc / "relatorios" / "custo-parcial-fase-0.md").read_text(encoding="utf-8")
    for texto in (completo, parcial):
        assert "preço não informado" in texto
        assert "Câmbio não informado" in texto
        assert "Preço não informado em `config/precos.yml` para: `claude-teste`" in texto
        assert "Desembolso real no período | não informado" in texto
        assert "0 tabelas e views, 0 rotinas" in texto
    assert "| 1 Catálogo | — |" in completo


def test_relatorio_com_precos(doc: Path, tmp_path: Path) -> None:
    (doc / "config" / "precos.yml").write_text(
        "modelos:\n  claude-teste:\n    entrada: 1\n    saida: 2\n    cache_escrita_5m: 3\n"
        "    cache_escrita_1h: 4\n    cache_leitura: 0.5\n    fonte: https://exemplo.invalido\n    consultado_em: 2026-10-23\n",
        encoding="utf-8",
    )
    (doc / "config" / "cambio.yml").write_text("ptax_venda: 5.0\nfonte: BCB\ndata: 2026-10-23\n", encoding="utf-8")
    (doc / "schemas" / "s" / "tabelas").mkdir(parents=True)
    (doc / "schemas" / "s" / "tabelas" / "t.md").write_text("x", encoding="utf-8")
    escrever_fases(doc, [LINHA_MANUAL.strip()])
    proj = tmp_path / "proj"
    escrever_logs(proj, "s1.jsonl", [linha("2026-10-23T12:00:00Z", "m1", entrada=1_000_000, saida=0)])
    coletar(doc, proj)
    assert main(["custos", "relatorio", "--doc", str(doc), "--workspace", str(tmp_path)]) == 0
    texto = (doc / "relatorios" / "custo-implementacao.md").read_text(encoding="utf-8")
    # 1M entrada*1 + 120 escrita5m*3 + 180 escrita1h*4 + 3000... = 1 + 0,00108 + 0,0015 -> US$ 1,00
    assert "US$ 1,00" in texto
    assert "preço não informado" not in texto
    assert "1 tabelas e views" in texto


def test_parcial_exige_fase(doc: Path, tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    coletar(doc, proj)
    assert main(["custos", "relatorio", "--parcial", "--doc", str(doc)]) == 2
