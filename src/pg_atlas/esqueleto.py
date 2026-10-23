"""Comando `esqueleto`: gera as fichas mecânicas em Markdown a partir do catálogo (decisão D-013).

Não conecta ao banco: lê só `<doc>/catalogo/` e `<doc>/config/`. Grava:
- `schemas/<schema>/README.md`, `tabelas/<tabela>.md`, `funcoes/<rotina>.md`;
- `llms.txt` provisório;
- `catalogo/_fichas.json` (objeto -> caminho) e `_controle/esqueleto-orfas.json`;
- o item `fases["1"]["tarefas"]["1.4"]` de `_controle/estado.json`.

Arquivo novo é escrito inteiro; arquivo existente só tem reescrito o que está entre marcadores.
Arquivo com marcadores quebrados não é tocado e o comando termina com erro.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pg_atlas.cli import adicionar_argumentos_comuns
from pg_atlas.config import carregar_projeto
from pg_atlas.erros import ErroConfig
from pg_atlas.esqueleto_contexto import Contexto, construir
from pg_atlas.esqueleto_fichas import (
    SECOES_README,
    SECOES_ROTINA,
    Bloco,
    conteudo_llms,
    ficha_readme,
    ficha_relacao,
    ficha_rotina,
    secoes_da_relacao,
)
from pg_atlas.esqueleto_marcadores import ErroMarcadores, atualizar, montar_novo
from pg_atlas.esqueleto_nomes import ErroEsqueleto, caminho_readme, montar_mapa
from pg_atlas.json_util import escrever_json
from pg_atlas.triagem import escrever_texto

FASE = "1"
TAREFA = "1.4"
MAPA = "_fichas.json"
ORFAS = "esqueleto-orfas.json"
LLMS = "llms.txt"
VERSAO_FORMATO = 1


@dataclass
class Resultado:
    """O que o esqueleto fez (contagens e mensagens para a saída)."""

    criados: int = 0
    atualizados: int = 0
    inalterados: int = 0
    erros: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    orfas: list[str] = field(default_factory=list)
    por_tipo: dict[str, int] = field(default_factory=dict)
    readmes: int = 0


def sincronizar(doc: Path, caminho: str, blocos: list[Bloco], resultado: Resultado) -> None:
    """Cria o arquivo ou reescreve as seções automáticas; marcadores quebrados só geram erro."""
    destino = doc / caminho
    if not destino.exists():
        escrever_texto(destino, montar_novo(blocos))
        resultado.criados += 1
        return
    try:
        atual = destino.read_text(encoding="utf-8-sig")
        novo, avisos = atualizar(atual, [(nome, texto) for nome, texto in blocos if nome])
    except (ErroMarcadores, UnicodeDecodeError) as erro:
        resultado.erros.append(f"{caminho}: {erro}")
        return
    resultado.avisos += [f"{caminho}: {a}" for a in avisos]
    if novo == atual and not destino.read_bytes().startswith(b"\xef\xbb\xbf"):
        resultado.inalterados += 1
        return
    escrever_texto(destino, novo)
    resultado.atualizados += 1


def montar_arquivos(ctx: Contexto, resultado: Resultado) -> dict[str, list[Bloco]]:
    """Caminho -> blocos de cada arquivo a gerar."""
    arquivos: dict[str, list[Bloco]] = {}
    for schema in ctx.schemas:
        arquivos[caminho_readme(schema)] = ficha_readme(ctx, schema)
    for chave in ctx.relacoes:
        arquivos[ctx.caminho_relacao[chave]] = ficha_relacao(ctx, chave)
        r = ctx.relacoes[chave]
        rotulo = "particao" if r.get("particao") else r["tipo"]
        resultado.por_tipo[rotulo] = resultado.por_tipo.get(rotulo, 0) + 1
    for objeto in ctx.funcoes:
        arquivos[ctx.caminhos[objeto]] = ficha_rotina(ctx, objeto)
        rotulo = objeto.tipo
        resultado.por_tipo[rotulo] = resultado.por_tipo.get(rotulo, 0) + 1
    resultado.readmes = len(ctx.schemas)
    return arquivos


def encontrar_orfas(doc: Path, esperados: set[str]) -> list[str]:
    """Fichas em `tabelas/` ou `funcoes/` sem objeto no mapa. Nunca são apagadas nem movidas."""
    raiz = doc / "schemas"
    if not raiz.is_dir():
        return []
    orfas = []
    for pasta in ("tabelas", "funcoes"):
        for arquivo in raiz.glob(f"*/{pasta}/*.md"):
            relativo = arquivo.relative_to(doc).as_posix()
            if relativo not in esperados:
                orfas.append(relativo)
    return sorted(orfas)


def gravar_estado(caminho: Path, resultado: Resultado) -> None:
    """Atualiza só `fases["1"]["tarefas"]["1.4"]`, preservando o resto do arquivo."""
    estado: dict[str, Any] = {}
    if caminho.is_file():
        try:
            lido = json.loads(caminho.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raise ErroConfig(f"JSON inválido em {caminho}") from None
        estado = lido if isinstance(lido, dict) else {}
    bloco = estado.setdefault("fases", {}).setdefault(FASE, {}).setdefault("tarefas", {}).setdefault(TAREFA, {})
    bloco["status"] = "concluida" if not resultado.erros else "com_erros"
    bloco["fichas_por_tipo"] = dict(sorted(resultado.por_tipo.items()))
    bloco["readmes"] = resultado.readmes
    bloco["arquivos"] = {
        "atualizados": resultado.atualizados, "criados": resultado.criados, "inalterados": resultado.inalterados,
    }
    bloco["orfas"] = len(resultado.orfas)
    bloco["erros_de_marcadores"] = len(resultado.erros)
    escrever_json(caminho, estado)


def executar_esqueleto(doc: Path, controle: Path, nome: str, descricao: str) -> Resultado:
    ctx = construir(doc, nome, descricao)  # falha aqui (colisão, catálogo ausente) antes de gerar qualquer coisa
    resultado = Resultado()
    arquivos = montar_arquivos(ctx, resultado)
    escrever_json(doc / "catalogo" / MAPA, {
        "formato": "fichas", "versao_formato": VERSAO_FORMATO, "fichas": montar_mapa(ctx.caminhos),
    })
    for caminho in sorted(arquivos):
        sincronizar(doc, caminho, arquivos[caminho], resultado)
    sincronizar(doc, LLMS, [("llms", conteudo_llms(ctx))], resultado)
    resultado.orfas = encontrar_orfas(doc, set(arquivos))
    escrever_json(controle / ORFAS, {"formato": "esqueleto-orfas", "versao_formato": VERSAO_FORMATO,
                                     "orfas": resultado.orfas})
    gravar_estado(controle / "estado.json", resultado)
    return resultado


def registrar(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "esqueleto", help="gera as fichas mecânicas em Markdown a partir do catálogo (sem conectar ao banco)"
    )
    adicionar_argumentos_comuns(parser)
    parser.set_defaults(func=executar)


def executar(args: argparse.Namespace) -> int:
    projeto = carregar_projeto(args.doc, args.workspace)
    resultado = executar_esqueleto(projeto.doc, projeto.controle, projeto.nome, projeto.descricao)
    fichas = sum(resultado.por_tipo.values())
    print(
        f"Esqueleto: {fichas} fichas e {resultado.readmes} READMEs "
        f"({resultado.criados} criados, {resultado.atualizados} atualizados, "
        f"{resultado.inalterados} inalterados)."
    )
    for aviso in resultado.avisos:
        print(f"Aviso: {aviso}")
    if resultado.orfas:
        print(f"Fichas órfãs: {len(resultado.orfas)} (lista em _controle/{ORFAS}).")
    for erro in resultado.erros:
        print(f"Erro: {erro}", file=sys.stderr)
    if resultado.erros:
        print(f"{len(resultado.erros)} arquivo(s) com marcadores quebrados não foram tocados.", file=sys.stderr)
        return ErroEsqueleto.codigo_saida
    return 0
