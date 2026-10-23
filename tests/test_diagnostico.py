"""Comando diagnostico: comportamento sem .env (offline) e com banco (marcado)."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import ENV_FALSO, escrever_env
from pg_atlas.cli import main


def test_sem_env_falha_com_mensagem_clara(doc: Path, workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    codigo = main(["diagnostico", "--doc", str(doc)])
    saida = capsys.readouterr()
    assert codigo != 0
    assert str(workspace / ".env") in saida.err
    assert ".env.example" in saida.err
    assert "PGPASSWORD" in saida.err


def test_variavel_vazia_falha_apontando_o_arquivo(doc: Path, workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    escrever_env(workspace, {**ENV_FALSO, "PGUSER": ""})
    codigo = main(["diagnostico", "--doc", str(doc)])
    erro = capsys.readouterr().err
    assert codigo != 0
    assert str(workspace / ".env") in erro and "PGUSER" in erro
    for valor in ENV_FALSO.values():
        assert valor not in erro


def test_workspace_sobrescrito(doc: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    outra = tmp_path / "outra-pasta"
    outra.mkdir()
    codigo = main(["diagnostico", "--doc", str(doc), "--workspace", str(outra)])
    assert codigo != 0
    assert str(outra / ".env") in capsys.readouterr().err


def test_doc_inexistente(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["diagnostico", "--doc", str(tmp_path / "nao-existe")]) != 0
    assert "não encontrada" in capsys.readouterr().err


def test_versao_e_comando_obrigatorio(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as saida:
        main(["--version"])
    assert saida.value.code == 0
    assert "pg_atlas" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main([])


@pytest.mark.banco
def test_diagnostico_com_banco_real(capsys: pytest.CaptureFixture[str]) -> None:
    """Roda contra o banco configurado no .env da pasta de trabalho real.

    Uso: `pytest -m banco --doc-real <pasta>` não existe; defina PG_ATLAS_DOC_REAL.
    """
    import os

    pasta = os.environ.get("PG_ATLAS_DOC_REAL")
    if not pasta:
        pytest.skip("defina PG_ATLAS_DOC_REAL com a pasta do repositório de documentação")
    assert main(["diagnostico", "--doc", pasta]) == 0
    assert "Sessão somente leitura: sim" in capsys.readouterr().out
