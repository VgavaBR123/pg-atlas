"""CLI `python -m pg_atlas <comando>`.

Para acrescentar um comando: crie um módulo com `registrar(subparsers)` que adicione
o subparser (use `adicionar_argumentos_comuns`) e defina `set_defaults(func=...)`,
onde `func(args) -> int`; depois inclua o nome do módulo em `MODULOS_DE_COMANDOS`.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Sequence
from pathlib import Path

from pg_atlas import __version__
from pg_atlas.erros import ErroAtlas

MODULOS_DE_COMANDOS = ("pg_atlas.diagnostico", "pg_atlas.custos", "pg_atlas.catalogo")


def adicionar_argumentos_comuns(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--doc",
        required=True,
        type=Path,
        help="pasta do repositório de documentação (contém config/projeto.yml)",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="pasta de trabalho, onde fica o .env (padrão: pasta-mãe de --doc)",
    )


def criar_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pg_atlas", description="Documenta bancos PostgreSQL.")
    parser.add_argument("--version", action="version", version=f"pg_atlas {__version__}")
    subparsers = parser.add_subparsers(dest="comando", metavar="comando", required=True)
    for nome_modulo in MODULOS_DE_COMANDOS:
        importlib.import_module(nome_modulo).registrar(subparsers)
    return parser


def _tornar_saida_tolerante() -> None:
    """Evita UnicodeEncodeError em consoles com codificação limitada."""
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def main(argv: Sequence[str] | None = None) -> int:
    _tornar_saida_tolerante()
    args = criar_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ErroAtlas as erro:
        print(f"Erro: {erro}", file=sys.stderr)
        return erro.codigo_saida
    except KeyboardInterrupt:
        print("Interrompido.", file=sys.stderr)
        return 130
