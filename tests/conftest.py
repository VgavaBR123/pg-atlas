"""Fixtures dos testes offline. Os `.env` usados aqui são sempre falsos, em pastas temporárias."""

from __future__ import annotations

from pathlib import Path

import pytest

PROJETO_YML = """\
projeto:
  nome: "Projeto de teste"
execucao:
  statement_timeout_ms: 45000
  lock_timeout_ms: 5000
"""

ENV_FALSO = {
    "PGHOST": "host.invalido.teste",
    "PGPORT": "5432",
    "PGDATABASE": "banco_falso",
    "PGUSER": "usuario_falso",
    "PGPASSWORD": "senha-falsa-123",
}


def escrever_env(workspace: Path, valores: dict[str, str]) -> Path:
    arquivo = workspace / ".env"
    arquivo.write_text("".join(f"{k}={v}\n" for k, v in valores.items()), encoding="utf-8")
    return arquivo


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    pasta = tmp_path / "trabalho"
    pasta.mkdir()
    return pasta


@pytest.fixture
def doc(workspace: Path) -> Path:
    pasta = workspace / "doc-teste"
    (pasta / "config").mkdir(parents=True)
    (pasta / "config" / "projeto.yml").write_text(PROJETO_YML, encoding="utf-8")
    return pasta


@pytest.fixture(autouse=True)
def sem_pgservice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PGSERVICE", raising=False)
