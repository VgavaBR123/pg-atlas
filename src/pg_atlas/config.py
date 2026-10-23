"""Leitura de `<doc>/config/projeto.yml` e resolução das pastas do trabalho."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from pg_atlas.erros import ErroConfig

STATEMENT_TIMEOUT_PADRAO_MS = 60_000
LOCK_TIMEOUT_PADRAO_MS = 5_000
TAXA_CONFIRMADO_PADRAO = 0.95
TAXA_HIPOTESE_PADRAO = 0.50
AMOSTRA_MAX_PADRAO = 1_000
AMOSTRA_MIN_PADRAO = 30


@dataclass(frozen=True)
class Projeto:
    """Configuração do projeto de documentação (valores com padrão aplicado)."""

    doc: Path
    workspace: Path
    dados: dict[str, Any] = field(default_factory=dict)

    @property
    def nome(self) -> str:
        return str(self._secao("projeto").get("nome", ""))

    @property
    def descricao(self) -> str:
        return str(self._secao("projeto").get("descricao", ""))

    @property
    def statement_timeout_ms(self) -> int:
        return self._inteiro("execucao", "statement_timeout_ms", STATEMENT_TIMEOUT_PADRAO_MS)

    @property
    def lock_timeout_ms(self) -> int:
        return self._inteiro("execucao", "lock_timeout_ms", LOCK_TIMEOUT_PADRAO_MS)

    @property
    def taxa_confirmado(self) -> float:
        return self._decimal_verificacoes("taxa_confirmado", TAXA_CONFIRMADO_PADRAO)

    @property
    def taxa_hipotese(self) -> float:
        return self._decimal_verificacoes("taxa_hipotese", TAXA_HIPOTESE_PADRAO)

    @property
    def amostra_max_valores(self) -> int:
        return self._inteiro_verificacoes("amostra_max_valores", AMOSTRA_MAX_PADRAO)

    @property
    def amostra_min_valores(self) -> int:
        return self._inteiro_verificacoes("amostra_min_valores", AMOSTRA_MIN_PADRAO)

    @property
    def controle(self) -> Path:
        return self.doc / "_controle"

    def _secao(self, nome: str) -> dict[str, Any]:
        valor = self.dados.get(nome) or {}
        if not isinstance(valor, dict):
            raise ErroConfig(f"projeto.yml: a seção '{nome}' deve ser um mapa.")
        return valor

    def _inteiro(self, secao: str, chave: str, padrao: int) -> int:
        valor = self._secao(secao).get(chave)
        if valor is None:
            return padrao
        if isinstance(valor, bool) or not isinstance(valor, int) or valor <= 0:
            raise ErroConfig(f"projeto.yml: '{secao}.{chave}' deve ser um inteiro positivo.")
        return valor

    def _bloco_verificacoes(self) -> dict[str, Any]:
        valor = self._secao("execucao").get("verificacoes") or {}
        if not isinstance(valor, dict):
            raise ErroConfig("projeto.yml: 'execucao.verificacoes' deve ser um mapa.")
        return valor

    def _inteiro_verificacoes(self, chave: str, padrao: int) -> int:
        valor = self._bloco_verificacoes().get(chave)
        if valor is None:
            return padrao
        if isinstance(valor, bool) or not isinstance(valor, int) or valor <= 0:
            raise ErroConfig(f"projeto.yml: 'execucao.verificacoes.{chave}' deve ser um inteiro positivo.")
        return valor

    def _decimal_verificacoes(self, chave: str, padrao: float) -> float:
        """Taxa entre 0 (exclusivo) e 1 (inclusivo); aceita inteiro ou decimal."""
        valor = self._bloco_verificacoes().get(chave)
        if valor is None:
            return padrao
        if isinstance(valor, bool) or not isinstance(valor, (int, float)) or not 0 < valor <= 1:
            raise ErroConfig(f"projeto.yml: 'execucao.verificacoes.{chave}' deve ser um número entre 0 e 1.")
        return float(valor)


def resolver_workspace(doc: Path, workspace: Path | None = None) -> Path:
    """A pasta de trabalho padrão é a pasta-mãe do repositório de documentação."""
    if workspace is not None:
        return Path(workspace).resolve()
    return Path(doc).resolve().parent


def carregar_projeto(doc: Path | str, workspace: Path | str | None = None) -> Projeto:
    pasta_doc = Path(doc).resolve()
    arquivo = pasta_doc / "config" / "projeto.yml"
    if not pasta_doc.is_dir():
        raise ErroConfig(f"Pasta de documentação não encontrada: {pasta_doc}")
    if not arquivo.is_file():
        raise ErroConfig(f"Arquivo de configuração não encontrado: {arquivo}")
    try:
        dados = yaml.safe_load(arquivo.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        raise ErroConfig(f"YAML inválido em {arquivo}") from None
    if not isinstance(dados, dict):
        raise ErroConfig(f"{arquivo} deve conter um mapa na raiz.")
    return Projeto(
        doc=pasta_doc,
        workspace=resolver_workspace(pasta_doc, Path(workspace) if workspace else None),
        dados=dados,
    )
