"""Formatação de números e Markdown das fichas do esqueleto (decisão D-013, item 7).

Estatísticas voláteis entram arredondadas para reduzir ruído no diff.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Sequence

TRAVESSAO = "—"
LIMITE_BLOCO_BYTES = 2 * 1024 * 1024
_UNIDADES = ("KB", "MB", "GB", "TB")


def milhar(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def linhas_arredondadas(n: int | None) -> str:
    """2 algarismos significativos, com separador de milhar."""
    if n is None:
        return TRAVESSAO
    if n < 100:
        return milhar(n)
    fator = 10 ** (len(str(n)) - 2)
    return milhar((n + fator // 2) // fator * fator)


def tamanho_legivel(bytes_: int | None) -> str:
    """Unidade legível com 1 casa decimal (base 1024)."""
    if bytes_ is None:
        return TRAVESSAO
    if bytes_ < 1024:
        return f"{bytes_} B"
    valor = float(bytes_)
    for unidade in _UNIDADES:
        valor /= 1024
        if valor < 1024 or unidade == _UNIDADES[-1]:
            return f"{valor:.1f}".replace(".", ",") + f" {unidade}"
    raise AssertionError("inalcançável")


def percentual(fracao: float | None) -> str:
    """Fração (0 a 1) como percentual com 1 casa."""
    if fracao is None:
        return TRAVESSAO
    return f"{fracao * 100:.1f}".replace(".", ",") + "%"


def distintos(n_distinct: float | None) -> str:
    """Positivo: inteiro. Negativo: fração das linhas."""
    if n_distinct is None:
        return TRAVESSAO
    if n_distinct >= 0:
        return milhar(int(round(n_distinct)))
    return "≈" + f"{-n_distinct * 100:.1f}".replace(".", ",").removesuffix(",0") + "% das linhas"


def linha_unica(texto: str) -> str:
    """Quebras de linha viram espaço."""
    return re.sub(r"\r\n|\r|\n", " ", texto)


def celula(texto: object) -> str:
    """Texto seguro para célula de tabela: `|` escapado e sem quebra de linha."""
    return linha_unica(str(texto)).replace("|", "\\|")


def codigo_inline(texto: str) -> str:
    """Trecho de código numa linha; sem crase dentro, vai entre crases."""
    limpo = linha_unica(texto)
    return limpo if "`" in limpo else f"`{limpo}`"


def tabela(cabecalho: Sequence[str], linhas: Sequence[Sequence[object]]) -> str:
    saida = [
        "| " + " | ".join(cabecalho) + " |",
        "|" + "|".join("---" for _ in cabecalho) + "|",
    ]
    saida += ["| " + " | ".join(celula(c) for c in linha) + " |" for linha in linhas]
    return "\n".join(saida)


def normalizar_quebras(texto: str) -> str:
    return texto.replace("\r\n", "\n").replace("\r", "\n")


def bloco_codigo(texto: str, linguagem: str = "sql") -> str:
    """Bloco cercado com mais crases que a maior sequência de crases do conteúdo."""
    corpo = normalizar_quebras(texto).rstrip("\n")
    maior = max((len(m) for m in re.findall(r"`+", corpo)), default=0)
    cerca = "`" * max(3, maior + 1)
    return f"{cerca}{linguagem}\n{corpo}\n{cerca}"


def bloco_codigo_limitado(texto: str, onde: str, linguagem: str = "sql") -> str:
    """Como `bloco_codigo`, mas acima de 2 MB troca o bloco por um aviso (D-013)."""
    if len(texto.encode("utf-8")) > LIMITE_BLOCO_BYTES:
        linhas = normalizar_quebras(texto).count("\n") + 1
        return f"_Bloco omitido por exceder 2 MB ({milhar(linhas)} linhas). Ver {onde}._"
    return bloco_codigo(texto, linguagem)


def link(origem: str, destino: str, texto: str) -> str:
    """Link Markdown relativo entre dois caminhos da raiz do repositório de documentação."""
    relativo = posixpath.relpath(destino, posixpath.dirname(origem) or ".")
    return f"[{texto}]({relativo})"
