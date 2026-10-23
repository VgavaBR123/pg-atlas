"""Extração dos comandos SQL de definições de views e corpos de rotinas (decisão D-018, item 2).

Offline e genérico. Entrada: texto já presente no catálogo (`definicao` de view, `codigo` de rotina).
Saída: lista de comandos SQL estáticos prontos para o analisador de joins. Nada é gravado aqui.

- Rotinas `sql`: corpo entre os delimitadores `$...$`, dividido em comandos por `;` fora de strings,
  identificadores entre aspas, dollar-quotes e comentários.
- Rotinas `plpgsql`: com `pglast`, os comandos SQL estáticos vêm da árvore de `parse_plpgsql`; sem
  `pglast` (ou se o parse falhar), um extrator heuristico sobre o corpo.
- `EXECUTE` (inclusive `RETURN QUERY EXECUTE`, `FOR ... IN EXECUTE`, `OPEN ... FOR EXECUTE`) fica
  fora e é só contado.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

PALAVRAS_COMANDO = ("SELECT", "WITH", "INSERT", "UPDATE", "DELETE")
EXTRATOR_PGLAST = "pglast"
EXTRATOR_HEURISTICO = "heuristico"

_ABERTURA_DOLLAR = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
_TOKEN = re.compile(
    r"""(?P<linha>--[^\n]*)|(?P<bloco>/\*)|(?P<texto>'(?:[^']|'')*')|(?P<ident>"(?:[^"]|"")*")"""
    r"""|(?P<dollar>\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$)|(?P<ponto>;)"""
)
_PRIMEIRA_PALAVRA = re.compile(r"^\s*\(*\s*([A-Za-z_]+)")
_ROTULO = re.compile(r"^\s*<<\s*\w+\s*>>\s*")
_FLUXO = re.compile(
    r"^\s*(?:(?:IF|ELSIF|ELSEIF|WHEN|WHILE)\b.*?\b(?:THEN|LOOP)\b|(?:BEGIN|THEN|ELSE|LOOP)\b)",
    re.IGNORECASE | re.DOTALL,
)
_RETURN_QUERY = re.compile(r"^\s*RETURN\s+QUERY\b", re.IGNORECASE)
_PERFORM = re.compile(r"^\s*PERFORM\b", re.IGNORECASE)
_FOR_IN = re.compile(r"^\s*FOR\s+\S+\s+IN\b", re.IGNORECASE)
_OPEN_FOR = re.compile(r"^\s*OPEN\s+\S+\s+(?:(?:NO\s+)?SCROLL\s+)?FOR\b", re.IGNORECASE)
_LOOP = re.compile(r"\bLOOP\b", re.IGNORECASE)
_INTO_STRICT = re.compile(r"\bINTO\s+STRICT\b", re.IGNORECASE)
_INTO = re.compile(
    r"\bINTO\s+(?:STRICT\s+)?[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*(?:\s*,\s*[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*)*",
    re.IGNORECASE,
)
_EXECUTE = re.compile(r"^\s*EXECUTE\b", re.IGNORECASE)
_PALAVRA_SELECT = re.compile(r"\bselect\b", re.IGNORECASE)
_ALVO_ATRIBUICAO = re.compile(r"^\s*[\w.\[\]\"$ ]+?\s*:?=\s*", re.DOTALL)

# (tipo de comando PL/pgSQL, chave) em que o texto é um comando SQL completo, não uma expressão.
_CONTEXTOS_COMANDO = {
    ("PLpgSQL_stmt_execsql", "sqlstmt"),
    ("PLpgSQL_stmt_perform", "expr"),
    ("PLpgSQL_stmt_return_query", "query"),
    ("PLpgSQL_stmt_fors", "query"),
    ("PLpgSQL_stmt_open", "query"),
}
_DINAMICOS = ("PLpgSQL_stmt_dynexecute", "PLpgSQL_stmt_dynfors")


@dataclass
class Extracao:
    """Comandos SQL estáticos de um objeto, os `EXECUTE` contados e se houve fallback."""

    comandos: list[str] = field(default_factory=list)
    execute_dinamico: int = 0
    fallback: str | None = None  # motivo da queda do pglast para o heuristico


def pglast_disponivel() -> bool:
    try:
        import pglast  # noqa: F401
    except ImportError:
        return False
    return True


# ----------------------------------------------------------------- texto

def _fim_comentario_bloco(texto: str, pos: int) -> int:
    """Posição logo após o `*/` que fecha o comentário (aninhado) aberto em `pos` (após o `/*`)."""
    nivel = 1
    i = pos
    while i < len(texto) and nivel:
        if texto.startswith("/*", i):
            nivel, i = nivel + 1, i + 2
        elif texto.startswith("*/", i):
            nivel, i = nivel - 1, i + 2
        else:
            i += 1
    return i


def dividir_comandos(texto: str) -> list[str]:
    """Divide em comandos por `;` fora de strings, identificadores, dollar-quotes e comentários.

    Os comentários são removidos (viram um espaço). Comandos vazios são descartados.
    """
    comandos: list[str] = []
    partes: list[str] = []
    pos = 0
    fim = len(texto)
    while pos < fim:
        m = _TOKEN.search(texto, pos)
        if m is None:
            partes.append(texto[pos:])
            break
        partes.append(texto[pos : m.start()])
        pos = m.end()
        if m.group("linha"):
            partes.append(" ")
        elif m.group("bloco"):
            pos = _fim_comentario_bloco(texto, pos)
            partes.append(" ")
        elif m.group("dollar"):
            tag = m.group("dollar")
            fecho = texto.find(tag, pos)
            pos = fim if fecho < 0 else fecho + len(tag)
            partes.append(texto[m.start() : pos])
        elif m.group("ponto"):
            comando = "".join(partes).strip()
            if comando:
                comandos.append(comando)
            partes = []
        else:
            partes.append(m.group(0))
    resto = "".join(partes).strip()
    if resto:
        comandos.append(resto)
    return comandos


def corpo_da_rotina(codigo: str) -> str | None:
    """Texto entre o primeiro delimitador `$tag$` e o próximo igual; `None` se não houver."""
    m = _ABERTURA_DOLLAR.search(codigo)
    if m is None:
        return None
    tag = m.group(0)
    fecho = codigo.find(tag, m.end())
    return codigo[m.end() :] if fecho < 0 else codigo[m.end() : fecho]


def primeira_palavra(comando: str) -> str:
    m = _PRIMEIRA_PALAVRA.match(comando)
    return m.group(1).upper() if m else ""


def eh_comando_sql(comando: str) -> bool:
    return primeira_palavra(comando) in PALAVRAS_COMANDO


def remover_into(comando: str) -> str:
    """Tira o alvo `INTO <variáveis>` de um SELECT (em qualquer posição); `INSERT INTO` fica intacto."""
    if primeira_palavra(comando) not in ("SELECT", "WITH"):
        return comando

    def trocar(m: re.Match[str]) -> str:
        antes = comando[: m.start()].rstrip().upper()
        return m.group(0) if antes.endswith("INSERT") else " "

    return _INTO.sub(trocar, comando)


def motivo_da_falha_pglast(erro: Exception) -> str:
    """Classifica a falha do `parse_plpgsql` em um motivo curto, sem reproduzir trecho de código."""
    texto = str(erro)
    for trecho, motivo in (
        ("LookupExplicitNamespace", "tipo_qualificado"),
        ("not a known variable", "variavel_desconhecida"),
        ("cannot accept type", "tipo_nao_suportado"),
        ("syntax error", "sintaxe"),
    ):
        if trecho in texto:
            return motivo
    return "outro"


# ----------------------------------------------------------------- heuristico


def _remover_prefixos_de_fluxo(trecho: str) -> str:
    for _ in range(8):
        novo = _FLUXO.sub("", _ROTULO.sub("", trecho, count=1), count=1)
        if novo == trecho:
            break
        trecho = novo
    return trecho


def _trecho_plpgsql(trecho: str) -> tuple[str | None, bool]:
    """Comando SQL estático de um trecho do corpo plpgsql (ou `None`) e se é um `EXECUTE`."""
    resto = _remover_prefixos_de_fluxo(trecho)
    if _RETURN_QUERY.match(resto):
        resto = _RETURN_QUERY.sub("", resto, count=1)
    elif _PERFORM.match(resto):
        resto = _PERFORM.sub("SELECT", resto, count=1)
    elif _FOR_IN.match(resto):
        resto = _FOR_IN.sub("", resto, count=1)
        corte = _LOOP.search(resto)
        resto = resto[: corte.start()] if corte else resto
    elif _OPEN_FOR.match(resto):
        resto = _OPEN_FOR.sub("", resto, count=1)
    resto = resto.strip()
    if _EXECUTE.match(resto):
        return None, True
    if not eh_comando_sql(resto):
        return None, False
    return _INTO_STRICT.sub("INTO", resto), False


def extrair_heuristico(corpo: str, linguagem: str) -> Extracao:
    """Comandos de uma rotina pelo corpo: divisão por `;` e aproveitamento dos trechos SQL."""
    extracao = Extracao()
    for trecho in dividir_comandos(corpo):
        if linguagem == "plpgsql":
            comando, execute = _trecho_plpgsql(trecho)
        else:
            comando, execute = (trecho, False) if eh_comando_sql(trecho) else (None, False)
        extracao.execute_dinamico += execute
        if comando is not None:
            extracao.comandos.append(comando)
    return extracao


# ----------------------------------------------------------------- pglast


def _texto_do_comando(consulta: str, contexto: tuple[str, str] | None, atribuicao: bool) -> str | None:
    """Comando SQL a partir do texto de uma `PLpgSQL_expr`; expressões com subconsulta ganham `SELECT `."""
    texto = consulta.strip()
    if atribuicao:
        texto = _ALVO_ATRIBUICAO.sub("", texto, count=1).strip()
    if eh_comando_sql(texto):
        return texto
    if contexto in _CONTEXTOS_COMANDO or not _PALAVRA_SELECT.search(texto):
        return None
    return f"SELECT {texto}"


def _coletar(no: Any, extracao: Extracao, comando: str | None, chave: str | None) -> None:
    """Percorre a árvore do `parse_plpgsql` coletando as `PLpgSQL_expr` estáticas."""
    if isinstance(no, list):
        for item in no:
            _coletar(item, extracao, comando, chave)
        return
    if not isinstance(no, dict):
        return
    for k, v in no.items():
        if k in _DINAMICOS:
            extracao.execute_dinamico += 1
            if k == "PLpgSQL_stmt_dynfors" and isinstance(v, dict):
                _coletar(v.get("body"), extracao, comando, None)
        elif k == "dynquery":
            extracao.execute_dinamico += bool(v)
        elif k == "PLpgSQL_expr":
            consulta = v.get("query") if isinstance(v, dict) else None
            if isinstance(consulta, str):
                texto = _texto_do_comando(
                    consulta,
                    (comando, chave) if comando and chave else None,
                    comando == "PLpgSQL_stmt_assign",
                )
                if texto is not None:
                    extracao.comandos.append(texto)
        elif k.startswith("PLpgSQL_stmt"):
            _coletar(v, extracao, k, None)
        else:
            _coletar(v, extracao, comando, k)


def extrair_pglast(codigo: str) -> Extracao:
    """Comandos SQL estáticos pela árvore do `pglast.parse_plpgsql`. Levanta exceção se o parse falhar."""
    import pglast

    extracao = Extracao()
    _coletar(pglast.parse_plpgsql(codigo), extracao, None, None)
    return extracao


# ----------------------------------------------------------------- entradas


def extrair_definicao(definicao: str) -> Extracao:
    """View ou materializada: a própria definição é o comando."""
    return Extracao(comandos=[definicao.strip()])


def extrair_rotina(codigo: str, linguagem: str, usar_pglast: bool) -> Extracao | None:
    """Comandos de uma rotina `sql` ou `plpgsql`; `None` se não houver corpo entre delimitadores."""
    corpo = corpo_da_rotina(codigo)
    if corpo is None:
        return None
    extracao: Extracao
    if linguagem == "plpgsql" and usar_pglast:
        try:
            extracao = extrair_pglast(codigo)
        except Exception as erro:  # parse_plpgsql falhou: cai no heuristico e guarda o motivo
            extracao = extrair_heuristico(corpo, linguagem)
            extracao.fallback = motivo_da_falha_pglast(erro)
    else:
        extracao = extrair_heuristico(corpo, linguagem)
    extracao.comandos = [remover_into(c) for c in extracao.comandos]
    return extracao
