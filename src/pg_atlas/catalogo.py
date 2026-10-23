"""Comando `catalogo`: extrai a estrutura e as estatísticas do banco, um schema por vez.

Saídas em `<doc>/catalogo/`: `estrutura/<schema>.json`, `estatisticas/<schema>.json` e
`_banco.json`. O progresso fica em `<doc>/_controle/estado.json`, dentro da tarefa 1.2
(chaves `progresso` e `schemas`; o campo `status` da tarefa não é tocado). Só as consultas
de `sql/catalogo/` e duas consultas leves de catálogo (versão e tamanho) são executadas,
todas por `Conexao`, numa única conexão.
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from pg_atlas import catalogo_montagem as montagem
from pg_atlas import catalogo_sql, mascara
from pg_atlas.cli import adicionar_argumentos_comuns
from pg_atlas.config import Projeto, carregar_projeto
from pg_atlas.conexao import Conexao
from pg_atlas.erros import ErroConfig
from pg_atlas.json_util import escrever_json

PASTA = "catalogo"
FASE = "1"
TAREFA = "1.2"
PENDENTE, CONCLUIDO, ERRO = "pendente", "concluido", "erro"
SQL_VERSAO = "SELECT version()"
SQL_TAMANHO = "SELECT pg_database_size(current_database())"
CONSULTAS_POR_SCHEMA = tuple(n for n in catalogo_sql.listar() if n != "schemas")


class FonteSql(Protocol):
    """O que o extrator precisa de `Conexao` (facilita testar sem banco)."""

    def executar_com_colunas(
        self, sql: str, params: Any = None
    ) -> tuple[list[str], list[tuple[Any, ...]]]: ...


# ----------------------------------------------------------------- consultas


def _dicionarios(resultado: tuple[list[str], list[tuple[Any, ...]]]) -> list[dict[str, Any]]:
    colunas, linhas = resultado
    return [dict(zip(colunas, linha)) for linha in linhas]


def consultar(fonte: FonteSql, nome: str, schema: str | None) -> list[dict[str, Any]]:
    return _dicionarios(
        fonte.executar_com_colunas(catalogo_sql.carregar(nome), catalogo_sql.parametros(schema))
    )


def listar_schemas(fonte: FonteSql, projeto: Projeto) -> list[str]:
    """Schemas no escopo da configuração, em ordem alfabética."""
    escopo = projeto.dados.get("escopo") or {}
    pedidos = escopo.get("schemas", "todos")
    excluir = set(escopo.get("excluir") or [])
    existentes = {l["schema"] for l in consultar(fonte, "schemas", None)}
    if pedidos != "todos":
        if not isinstance(pedidos, list):
            raise ErroConfig("projeto.yml: 'escopo.schemas' deve ser 'todos' ou uma lista.")
        existentes &= set(pedidos)
    return sorted(existentes - excluir)


def extrair_schema(
    fonte: FonteSql, schema: str, extraido_em: str, mascarador: mascara.Mascarador | None = None
) -> tuple[dict, dict, list[dict[str, Any]]]:
    """Roda as consultas do schema e devolve (estrutura mascarada, estatísticas, registros)."""
    linhas: montagem.Linhas = {"schemas": consultar(fonte, "schemas", schema)}
    for nome in CONSULTAS_POR_SCHEMA:
        linhas[nome] = consultar(fonte, nome, schema)
    estrutura, registros = mascara.mascarar_estrutura(
        montagem.montar_estrutura(schema, linhas), mascarador
    )
    return estrutura, montagem.montar_estatisticas(schema, linhas, extraido_em), registros


# ----------------------------------------------------------------- estado.json


def ler_json(caminho: Path) -> dict[str, Any]:
    if not caminho.is_file():
        return {}
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise ErroConfig(f"JSON inválido em {caminho}") from None
    return dados if isinstance(dados, dict) else {}


def _bloco_tarefa(estado: dict[str, Any]) -> dict[str, Any]:
    fase = estado.setdefault("fases", {}).setdefault(FASE, {})
    return fase.setdefault("tarefas", {}).setdefault(TAREFA, {})


def ler_mapa_schemas(estado: dict[str, Any]) -> dict[str, str]:
    bruto = _bloco_tarefa(estado).get("schemas")
    return dict(bruto) if isinstance(bruto, dict) else {}


def planejar(
    anterior: dict[str, str], todos: Sequence[str], alvo: Sequence[str], retomar: bool
) -> tuple[dict[str, str], list[str]]:
    """Decide o mapa de status por schema e quais schemas processar nesta execução.

    - Execução completa sem `--retomar`: tudo volta a pendente.
    - `--retomar`: mantém os concluídos (ainda no escopo) e pula-os; `erro` volta a pendente.
    - `--schema X` (alvo parcial) sem `--retomar`: reprocessa só X e preserva o resto.
    """
    completa = list(alvo) == list(todos)
    if not retomar and completa:
        base: dict[str, str] = {}
    else:
        base = {s: anterior.get(s, PENDENTE) for s in todos}
        base = {s: (PENDENTE if v == ERRO else v) for s, v in base.items()}
    mapa = {s: base.get(s, PENDENTE) for s in todos}
    for s in alvo:
        if not (retomar and mapa[s] == CONCLUIDO):
            mapa[s] = PENDENTE
    processar = [s for s in alvo if mapa[s] != CONCLUIDO]
    return dict(sorted(mapa.items())), processar


def gravar_progresso(caminho: Path, mapa: dict[str, str]) -> None:
    """Grava `progresso` e `schemas` na tarefa 1.2, sem mexer no resto (escrita atômica)."""
    estado = ler_json(caminho)
    bloco = _bloco_tarefa(estado)
    bloco["schemas"] = dict(sorted(mapa.items()))
    bloco["progresso"] = {
        "total": len(mapa),
        "concluidos": sum(1 for v in mapa.values() if v == CONCLUIDO),
    }
    escrever_json(caminho, estado)


# ----------------------------------------------------------------- arquivos


def _gravar_validado(caminho: Path, documento: dict[str, Any]) -> None:
    montagem.validar(documento, rotulo=str(caminho.name))
    escrever_json(caminho, documento)


def _contagens_anteriores(raiz: Path, mapa: dict[str, str]) -> dict[str, dict[str, int]]:
    """Contagens dos schemas já concluídos: do `_banco.json` ou, na falta, da estrutura."""
    banco = ler_json(raiz / "_banco.json").get("schemas")
    banco = banco if isinstance(banco, dict) else {}
    saida: dict[str, dict[str, int]] = {}
    for schema, status in mapa.items():
        if status != CONCLUIDO:
            continue
        if schema in banco:
            saida[schema] = dict(banco[schema])
            continue
        arquivo = raiz / "estrutura" / f"{schema}.json"
        if arquivo.is_file():
            saida[schema] = montagem.contar_objetos(ler_json(arquivo))
    return saida


PADROES_GITIGNORE_MAPA = (
    f"_controle/{mascara.NOME_MAPA}",
    mascara.NOME_MAPA,
    "_controle/*.local.json",
    "*.local.json",
)


def exigir_mapa_ignorado(doc: Path) -> None:
    """O mapa valor -> N nunca pode ir para o git: exige entrada no `.gitignore` antes de criá-lo."""
    arquivo = doc / ".gitignore"
    linhas = {l.strip() for l in arquivo.read_text(encoding="utf-8").splitlines()} if arquivo.is_file() else set()
    if not linhas & set(PADROES_GITIGNORE_MAPA):
        raise ErroConfig(
            f"Inclua '_controle/{mascara.NOME_MAPA}' no .gitignore de {doc.name} antes de extrair."
        )


def carregar_mascarador(controle: Path) -> mascara.Mascarador:
    bruto = ler_json(controle / mascara.NOME_MAPA)
    mapa = {t: {str(k): int(v) for k, v in (bruto.get(t) or {}).items()} for t in mascara.NUMERADOS}
    return mascara.Mascarador(mapa)


def salvar_mascarador(controle: Path, mascarador: mascara.Mascarador) -> None:
    if mascarador.alterado:
        escrever_json(controle / mascara.NOME_MAPA, mascarador.mapa)
        mascarador.alterado = False


def gravar_mascaramento(raiz_controle: Path, schema: str, registros: list[dict[str, Any]]) -> None:
    """Atualiza `_controle/mascaramento.json` trocando só as entradas do schema (sem valores)."""
    caminho = raiz_controle / "mascaramento.json"
    antigos = ler_json(caminho).get("registros")
    mantidos = [r for r in (antigos if isinstance(antigos, list) else []) if r.get("schema") != schema]
    todos = sorted(
        mantidos + registros,
        key=lambda r: (r["schema"], r["objeto"], r["campo"], r["tipo"]),
    )
    doc = {"formato": "mascaramento", "versao_formato": montagem.VERSAO_FORMATO, "registros": todos}
    montagem.validar(doc, rotulo="mascaramento.json")
    escrever_json(caminho, doc)


def verificar_mascara(raiz: Path, imprimir: Callable[[str], None] = print) -> int:
    """Varre os JSON de `catalogo/`; devolve o total de ocorrências não mascaradas.

    Imprime só arquivo, campo e tipo (nunca o valor).
    """
    total = 0
    for arquivo in sorted(raiz.rglob("*.json")):
        for caminho, contagem in mascara.varrer_documento(ler_json(arquivo)):
            for tipo, n in contagem.items():
                total += n
                imprimir(f"{arquivo.relative_to(raiz).as_posix()}: {caminho}: {tipo} x{n}")
    return total


# ----------------------------------------------------------------- execução


def agora_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def extrair(
    projeto: Projeto,
    fonte: FonteSql,
    *,
    schema: str | None = None,
    retomar: bool = False,
    relogio: Callable[[], str] = agora_utc,
    imprimir: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Extrai o catálogo. Devolve um resumo (`processados`, `pulados`, `totais`)."""
    raiz = projeto.doc / PASTA
    caminho_estado = projeto.controle / "estado.json"
    exigir_mapa_ignorado(projeto.doc)
    mascarador = carregar_mascarador(projeto.controle)
    todos = listar_schemas(fonte, projeto)
    if schema is not None and schema not in todos:
        raise ErroConfig(f"Schema '{schema}' não existe ou está fora do escopo do projeto.yml.")
    alvo = [schema] if schema else todos

    anterior = ler_mapa_schemas(ler_json(caminho_estado))
    mapa, processar = planejar(anterior, todos, alvo, retomar)
    por_schema = _contagens_anteriores(raiz, mapa)
    versao = str(fonte.executar_com_colunas(SQL_VERSAO)[1][0][0])
    tamanho = int(fonte.executar_com_colunas(SQL_TAMANHO)[1][0][0])
    extraido_em = relogio()
    gravar_progresso(caminho_estado, mapa)

    for i, nome in enumerate(processar, start=1):
        inicio = time.monotonic()
        try:
            estrutura, estatisticas, registros = extrair_schema(
                fonte, nome, extraido_em, mascarador
            )
            salvar_mascarador(projeto.controle, mascarador)
            _gravar_validado(raiz / "estrutura" / f"{nome}.json", estrutura)
            _gravar_validado(raiz / "estatisticas" / f"{nome}.json", estatisticas)
            gravar_mascaramento(projeto.controle, nome, registros)
            por_schema[nome] = montagem.contar_objetos(estrutura)
            _gravar_validado(
                raiz / "_banco.json", montagem.montar_banco(versao, tamanho, extraido_em, por_schema)
            )
        except BaseException:
            mapa[nome] = ERRO
            gravar_progresso(caminho_estado, mapa)
            raise
        mapa[nome] = CONCLUIDO
        gravar_progresso(caminho_estado, mapa)
        c = por_schema[nome]
        imprimir(
            f"[{i}/{len(processar)}] {nome}: {c['tabela'] + c['particionada']} tabelas, "
            f"{c['view'] + c['materializada']} views, {c['funcoes']} funções "
            f"({time.monotonic() - inicio:.1f}s)"
        )
    # Garante um `_banco.json` coerente mesmo quando nada foi processado (tudo retomado).
    _gravar_validado(
        raiz / "_banco.json", montagem.montar_banco(versao, tamanho, extraido_em, por_schema)
    )
    return {
        "processados": processar,
        "pulados": [s for s in alvo if s not in processar],
        "totais": montagem.somar_contagens(por_schema),
        "concluidos": sum(1 for v in mapa.values() if v == CONCLUIDO),
        "total": len(mapa),
    }


def registrar(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "catalogo", help="extrai o catálogo (estrutura e estatísticas), um schema por vez"
    )
    adicionar_argumentos_comuns(parser)
    parser.add_argument("--schema", default=None, help="extrai só este schema")
    parser.add_argument(
        "--retomar", action="store_true", help="pula os schemas já concluídos na extração em curso"
    )
    parser.add_argument(
        "--verificar-mascara",
        action="store_true",
        help="não conecta: varre catalogo/ e falha se houver dado pessoal não mascarado",
    )
    parser.set_defaults(func=executar)


def executar(args: argparse.Namespace) -> int:
    projeto = carregar_projeto(args.doc, args.workspace)
    if args.verificar_mascara:
        achados = verificar_mascara(projeto.doc / PASTA)
        print(f"Máscara: {achados} ocorrência(s) não mascarada(s) em catalogo/.")
        return 1 if achados else 0
    inicio = time.monotonic()
    with Conexao(projeto) as conexao:
        resumo = extrair(projeto, conexao, schema=args.schema, retomar=args.retomar)
    print(
        f"Catálogo: {resumo['concluidos']}/{resumo['total']} schemas concluídos "
        f"({len(resumo['processados'])} processados, {len(resumo['pulados'])} pulados) "
        f"em {time.monotonic() - inicio:.1f}s."
    )
    return 0
