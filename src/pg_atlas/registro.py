"""Registro (log) de todo SQL submetido, em JSON Lines, em `<doc>/_controle/`.

Guarda o texto do SQL e o resultado do guarda, nunca valores de conexão nem
parâmetros. A fase 1 usa este arquivo para provar que nenhuma tabela de usuário
foi lida.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

NOME_ARQUIVO = "sql.log.jsonl"


class RegistroSql:
    def __init__(self, caminho: Path) -> None:
        self.caminho = Path(caminho)
        self._seq = 0

    def _gravar(self, registro: dict[str, Any]) -> None:
        self._seq += 1
        registro = {
            "seq": self._seq,
            "em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **registro,
        }
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        with open(self.caminho, "a", encoding="utf-8", newline="\n") as arquivo:
            arquivo.write(json.dumps(registro, sort_keys=True, ensure_ascii=False) + "\n")

    def sql(self, sql: str, resultado: str, motivo: str | None = None) -> None:
        """`resultado`: 'rejeitado', 'executado' ou 'erro'."""
        registro: dict[str, Any] = {"tipo": "sql", "resultado": resultado, "sql": sql}
        if motivo:
            registro["motivo"] = motivo
        self._gravar(registro)

    def evento(self, mensagem: str) -> None:
        self._gravar({"tipo": "evento", "mensagem": mensagem})
