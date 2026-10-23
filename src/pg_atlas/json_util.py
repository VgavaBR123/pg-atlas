"""Escrita determinística de JSON: o `git diff` só mostra mudanças reais."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def serializar_json(dados: Any) -> str:
    """Chaves ordenadas, UTF-8 sem escape, indentação de 2 espaços, `\n` final.

    A ordem das listas é a do chamador (ordenar é responsabilidade dele,
    pois nem toda lista tem ordem livre).
    """
    return json.dumps(dados, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def escrever_json(caminho: Path | str, dados: Any) -> None:
    """Grava de forma atômica, em UTF-8, com fim de linha `\n` mesmo no Windows."""
    destino = Path(caminho)
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporario = destino.with_name(destino.name + ".tmp")
    with open(temporario, "w", encoding="utf-8", newline="\n") as arquivo:
        arquivo.write(serializar_json(dados))
    os.replace(temporario, destino)
