"""Leitura de configuração e escrita determinística de JSON."""

from __future__ import annotations

from pathlib import Path

import pytest

from pg_atlas.config import carregar_projeto, resolver_workspace
from pg_atlas.erros import ErroConfig
from pg_atlas.json_util import escrever_json, serializar_json


def test_le_projeto(doc: Path, workspace: Path) -> None:
    projeto = carregar_projeto(doc)
    assert projeto.nome == "Projeto de teste"
    assert projeto.statement_timeout_ms == 45000
    assert projeto.lock_timeout_ms == 5000
    assert projeto.workspace == workspace.resolve()
    assert projeto.controle == doc.resolve() / "_controle"


def test_padroes_quando_ausente(doc: Path) -> None:
    (doc / "config" / "projeto.yml").write_text("projeto:\n  nome: x\n", encoding="utf-8")
    projeto = carregar_projeto(doc)
    assert projeto.statement_timeout_ms == 60000
    assert projeto.lock_timeout_ms == 5000


def test_workspace_sobrescrito(doc: Path, tmp_path: Path) -> None:
    outra = tmp_path / "outra"
    assert carregar_projeto(doc, outra).workspace == outra.resolve()
    assert resolver_workspace(doc) == doc.resolve().parent


def test_arquivo_ausente_aponta_o_caminho(workspace: Path) -> None:
    pasta = workspace / "vazio"
    pasta.mkdir()
    with pytest.raises(ErroConfig) as erro:
        carregar_projeto(pasta)
    assert "projeto.yml" in str(erro.value)


def test_pasta_inexistente(tmp_path: Path) -> None:
    with pytest.raises(ErroConfig):
        carregar_projeto(tmp_path / "nao-existe")


@pytest.mark.parametrize("conteudo", ["execucao:\n  statement_timeout_ms: -1\n",
                                      "execucao:\n  statement_timeout_ms: abc\n",
                                      "execucao: 3\n", "- lista\n", "a: [\n"])
def test_config_invalida(doc: Path, conteudo: str) -> None:
    (doc / "config" / "projeto.yml").write_text(conteudo, encoding="utf-8")
    with pytest.raises(ErroConfig):
        carregar_projeto(doc).statement_timeout_ms  # noqa: B018


def test_json_deterministico() -> None:
    dados = {"b": 1, "a": {"z": [3, 1], "y": "ação"}}
    texto = serializar_json(dados)
    assert texto == '{\n  "a": {\n    "y": "ação",\n    "z": [\n      3,\n      1\n    ]\n  },\n  "b": 1\n}\n'
    assert serializar_json({"a": {"y": "ação", "z": [3, 1]}, "b": 1}) == texto


def test_escrever_json_utf8_lf(tmp_path: Path) -> None:
    destino = tmp_path / "sub" / "x.json"
    escrever_json(destino, {"nome": "João", "n": 2})
    bruto = destino.read_bytes()
    assert b"\r" not in bruto
    assert bruto.endswith(b"}\n")
    assert "João".encode("utf-8") in bruto
    escrever_json(destino, {"n": 2, "nome": "João"})
    assert destino.read_bytes() == bruto
    assert not list(tmp_path.glob("sub/*.tmp"))


def test_limiares_de_verificacao_tem_padrao_e_aceita_projeto(tmp_path) -> None:
    from pg_atlas.config import Projeto

    vazio = Projeto(doc=tmp_path, workspace=tmp_path, dados={})
    assert (vazio.taxa_confirmado, vazio.taxa_hipotese) == (0.95, 0.5)
    assert (vazio.amostra_max_valores, vazio.amostra_min_valores) == (1000, 30)
    cfg = {"execucao": {"verificacoes": {"taxa_confirmado": 0.9, "amostra_min_valores": 10}}}
    ajustado = Projeto(doc=tmp_path, workspace=tmp_path, dados=cfg)
    assert ajustado.taxa_confirmado == 0.9 and ajustado.amostra_min_valores == 10
    ruim = Projeto(doc=tmp_path, workspace=tmp_path, dados={"execucao": {"verificacoes": {"taxa_hipotese": 1.5}}})
    with pytest.raises(ErroConfig):
        ruim.taxa_hipotese
