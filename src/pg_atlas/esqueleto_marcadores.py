"""Marcadores de seção automática do esqueleto (decisão D-013, item 1 e 4).

Formato (cada marcador sozinho na sua linha):

    <!-- pg-atlas:auto inicio secao="<nome>" -->
    ...
    <!-- pg-atlas:auto fim -->

O nome usa só `[a-z0-9_]`, não se repete no arquivo e as seções não se aninham.
`atualizar` reescreve só o conteúdo entre marcadores; o texto de fora nunca é tocado.
Marcadores quebrados geram `ErroMarcadores` e o arquivo não deve ser gravado.
"""

from __future__ import annotations

import re

from pg_atlas.erros import ErroAtlas

_RE_INICIO = re.compile(r'^<!-- pg-atlas:auto inicio secao="([^"]*)" -->$')
_RE_FIM = re.compile(r"^<!-- pg-atlas:auto fim -->$")
_RE_NOME = re.compile(r"^[a-z0-9_]+$")
_TOKEN = "pg-atlas:auto"
_ZERO = "​"


class ErroMarcadores(ErroAtlas):
    """Marcadores quebrados em um arquivo existente."""

    codigo_saida = 4


Posicoes = dict[str, tuple[int, int]]  # nome -> (linha do início, linha do fim)


def inicio(nome: str) -> str:
    return f'<!-- pg-atlas:auto inicio secao="{nome}" -->'


FIM = "<!-- pg-atlas:auto fim -->"


def neutralizar(texto: str) -> str:
    """Impede que conteúdo gerado (ex.: código de rotina) seja lido como marcador ou pendência."""
    return texto.replace(_TOKEN, "pg-atlas:" + _ZERO + "auto").replace(
        "pg-atlas:pendente", "pg-atlas:" + _ZERO + "pendente"
    )


def envolver(nome: str, conteudo: str) -> str:
    """Conteúdo de uma seção já com os marcadores."""
    return f"{inicio(nome)}\n{conteudo}\n{FIM}"


def interpretar(linhas: list[str]) -> Posicoes:
    """Localiza as seções; levanta `ErroMarcadores` se algum marcador estiver quebrado."""
    posicoes: Posicoes = {}
    aberta: tuple[str, int] | None = None
    for i, linha in enumerate(linhas):
        if _TOKEN not in linha:
            continue
        texto = linha.strip()
        numero = i + 1
        achou_inicio, achou_fim = _RE_INICIO.match(texto), _RE_FIM.match(texto)
        if achou_inicio:
            aberta = _abrir(achou_inicio.group(1), aberta, posicoes, numero, i)
        elif achou_fim:
            if aberta is None:
                raise ErroMarcadores(f"marcador de fim sem início (linha {numero})")
            posicoes[aberta[0]] = (aberta[1], i)
            aberta = None
        else:
            raise ErroMarcadores(f"marcador mal formado (linha {numero})")
    if aberta is not None:
        raise ErroMarcadores(f"seção '{aberta[0]}' sem marcador de fim (início na linha {aberta[1] + 1})")
    return posicoes


def _abrir(
    nome: str, aberta: tuple[str, int] | None, posicoes: Posicoes, numero: int, indice: int
) -> tuple[str, int]:
    if not _RE_NOME.match(nome):
        raise ErroMarcadores(f"seção sem nome válido (linha {numero})")
    if aberta is not None:
        raise ErroMarcadores(f"seção aninhada: '{nome}' abre dentro de '{aberta[0]}' (linha {numero})")
    if nome in posicoes:
        raise ErroMarcadores(f"seção '{nome}' repetida (linha {numero})")
    return nome, indice


def montar_novo(blocos: list[tuple[str | None, str]]) -> str:
    """Arquivo novo: blocos `(nome, texto)`; `nome` None é texto fixo, senão seção automática."""
    partes = [envolver(nome, texto) if nome else texto for nome, texto in blocos]
    return "\n\n".join(partes) + "\n"


def atualizar(texto: str, secoes: list[tuple[str, str]]) -> tuple[str, list[str]]:
    """Reescreve as seções automáticas `secoes` (ordem canônica) num arquivo existente.

    Devolve (novo texto, avisos). Seção ausente é inserida na posição canônica;
    seção existente que não é mais gerada fica intacta, com aviso.
    """
    linhas = texto.split("\n")
    esperados = [nome for nome, _ in secoes]
    existentes = interpretar(linhas)
    avisos = [
        f"seção '{nome}' não é mais gerada para este objeto; mantida"
        for nome in existentes
        if nome not in esperados
    ]
    if all(nome in existentes for nome in esperados):  # caminho comum: só trocar o conteúdo
        for nome, conteudo in sorted(secoes, key=lambda s: existentes[s[0]][0], reverse=True):
            ini, fim = existentes[nome]
            linhas[ini : fim + 1] = envolver(nome, conteudo).split("\n")
        return _finalizar(linhas), avisos
    for posicao, (nome, conteudo) in enumerate(secoes):
        existentes = interpretar(linhas)
        bloco = envolver(nome, conteudo).split("\n")
        if nome in existentes:
            ini, fim = existentes[nome]
            linhas[ini : fim + 1] = bloco
        else:
            _inserir(linhas, bloco, esperados, posicao, existentes)
    return _finalizar(linhas), avisos


def _finalizar(linhas: list[str]) -> str:
    resultado = "\n".join(linhas)
    return resultado if resultado.endswith("\n") else resultado + "\n"


def _inserir(
    linhas: list[str], bloco: list[str], esperados: list[str], posicao: int, existentes: Posicoes
) -> None:
    """Insere a seção ausente na posição canônica (D-013, item 4)."""
    for anterior in reversed(esperados[:posicao]):
        if anterior in existentes:
            linhas[existentes[anterior][1] + 1 : existentes[anterior][1] + 1] = ["", *bloco]
            return
    for seguinte in esperados[posicao + 1 :]:
        if seguinte in existentes:
            ini = existentes[seguinte][0]
            linhas[ini:ini] = [*bloco, ""]
            return
    if esperados[posicao] == "cabecalho":
        linhas[0:0] = [*bloco, ""] if linhas != [""] else bloco
        return
    while linhas and linhas[-1] == "":
        linhas.pop()
    linhas += ["", *bloco, ""] if linhas else [*bloco, ""]
