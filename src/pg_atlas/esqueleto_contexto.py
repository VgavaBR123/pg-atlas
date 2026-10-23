"""Índices em memória do catálogo, usados para gerar as fichas (sem conexão, só leitura de arquivos)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from pg_atlas.erros import ErroConfig
from pg_atlas.esqueleto_nomes import (
    PASTA_RELACOES,
    PASTA_ROTINAS,
    ErroEsqueleto,
    Objeto,
    atribuir_caminhos,
)
from pg_atlas.triagem import carregar_catalogo

Chave = tuple[str, str]  # (schema, nome) de uma relação
TRIAGEM = "_triagem.json"


@dataclass
class Contexto:
    """Tudo o que as fichas precisam saber do catálogo, já indexado."""

    extraido_em: str
    projeto_nome: str
    projeto_descricao: str
    fatos: dict[str, Any]
    doc: Path
    schemas: list[str]
    estruturas: dict[str, dict[str, Any]]
    relacoes: dict[Chave, dict[str, Any]] = field(default_factory=dict)
    estatisticas: dict[Chave, dict[str, Any]] = field(default_factory=dict)
    triagem: dict[Chave, dict[str, Any]] = field(default_factory=dict)
    filhas: dict[Chave, list[Chave]] = field(default_factory=dict)
    funcoes: dict[Objeto, dict[str, Any]] = field(default_factory=dict)
    caminhos: dict[Objeto, str] = field(default_factory=dict)
    caminho_relacao: dict[Chave, str] = field(default_factory=dict)
    fk_entrada: dict[Chave, list[tuple[Chave, list[str], list[str]]]] = field(default_factory=dict)
    views_dependentes: dict[Chave, list[Chave]] = field(default_factory=dict)
    gatilhos_da_funcao: dict[Objeto, list[tuple[Chave, dict[str, Any]]]] = field(default_factory=dict)
    funcao_do_gatilho: dict[tuple[Chave, str], Objeto] = field(default_factory=dict)


def _ler_json(caminho: Path, aviso: str) -> dict[str, Any]:
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ErroEsqueleto(f"Não foi possível ler {caminho}. {aviso}") from None
    if not isinstance(dados, dict):
        raise ErroEsqueleto(f"{caminho} deveria conter um objeto JSON. {aviso}")
    return dados


def _ler_yaml(caminho: Path) -> dict[str, Any]:
    if not caminho.is_file():
        return {}
    try:
        dados = yaml.safe_load(caminho.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        raise ErroConfig(f"YAML inválido em {caminho}") from None
    return dados if isinstance(dados, dict) else {}


def objeto_da_funcao(schema: str, funcao: dict[str, Any]) -> Objeto:
    return Objeto(
        schema, PASTA_ROTINAS, funcao.get("tipo") or "function", funcao["nome"],
        funcao.get("argumentos_identidade") or "",
    )


def objeto_da_relacao(schema: str, relacao: dict[str, Any]) -> Objeto:
    return Objeto(schema, PASTA_RELACOES, relacao["tipo"], relacao["nome"])


def construir(doc: Path, projeto_nome: str, projeto_descricao: str) -> Contexto:
    """Lê o catálogo e a triagem e monta todos os índices. Falha se faltar algo."""
    raiz = doc / "catalogo"
    arquivo_triagem = raiz / TRIAGEM
    if not arquivo_triagem.is_file():
        raise ErroEsqueleto(f"Triagem ausente: {arquivo_triagem} não existe. Rode 'pg_atlas triagem'.")
    extraido_em, estruturas, estatisticas = carregar_catalogo(raiz)
    triagem = _ler_json(arquivo_triagem, "Rode 'pg_atlas triagem'.")
    ctx = Contexto(
        extraido_em=extraido_em,
        projeto_nome=projeto_nome,
        projeto_descricao=projeto_descricao,
        fatos=_ler_yaml(doc / "config" / "fatos.yml"),
        doc=doc,
        schemas=sorted(estruturas),
        estruturas=estruturas,
    )
    _indexar_basico(ctx, estatisticas, triagem)
    _indexar_caminhos(ctx)
    _indexar_ligacoes(ctx)
    return ctx


def _indexar_basico(
    ctx: Contexto, estatisticas: dict[str, dict[str, Any]], triagem: dict[str, Any]
) -> None:
    for schema in ctx.schemas:
        for r in ctx.estruturas[schema].get("relacoes", []):
            ctx.relacoes[(schema, r["nome"])] = r
        for f in ctx.estruturas[schema].get("funcoes", []):
            ctx.funcoes[objeto_da_funcao(schema, f)] = f
        for e in estatisticas[schema].get("relacoes", []):
            ctx.estatisticas[(schema, e["nome"])] = e
    for item in triagem.get("tabelas", []):
        ctx.triagem[(item["schema"], item["nome"])] = item
    for chave, r in sorted(ctx.relacoes.items()):
        pai = r.get("particao")
        if pai and pai.get("pai_nome"):
            ctx.filhas.setdefault((pai.get("pai_schema") or chave[0], pai["pai_nome"]), []).append(chave)


def _indexar_caminhos(ctx: Contexto) -> None:
    objetos = [objeto_da_relacao(s, r) for (s, _), r in ctx.relacoes.items()] + list(ctx.funcoes)
    ctx.caminhos = atribuir_caminhos(objetos)
    for (schema, nome), r in ctx.relacoes.items():
        ctx.caminho_relacao[(schema, nome)] = ctx.caminhos[objeto_da_relacao(schema, r)]


def _indexar_ligacoes(ctx: Contexto) -> None:
    """FKs de entrada, views dependentes e função de cada gatilho."""
    for chave, r in sorted(ctx.relacoes.items()):
        for restricao in r.get("restricoes", []):
            if restricao["tipo"] == "fk" and not restricao["herdada"] and restricao["ref_relacao"]:
                alvo = (restricao["ref_schema"] or chave[0], restricao["ref_relacao"])
                ctx.fk_entrada.setdefault(alvo, []).append(
                    (chave, restricao["colunas"], restricao["ref_colunas"])
                )
        if r["tipo"] in ("view", "materializada"):
            for dep in r.get("depende_de", []):
                ctx.views_dependentes.setdefault((dep["schema"], dep["relacao"]), []).append(chave)
        for gatilho in r.get("gatilhos", []):
            funcao = _funcao_do_gatilho(ctx, gatilho)
            if funcao is not None:
                ctx.funcao_do_gatilho[(chave, gatilho["nome"])] = funcao
                ctx.gatilhos_da_funcao.setdefault(funcao, []).append((chave, gatilho))


def _funcao_do_gatilho(ctx: Contexto, gatilho: dict[str, Any]) -> Objeto | None:
    """Função de gatilho: mesmo schema e nome; prefere a sem argumentos; ambiguidade -> sem link."""
    candidatas = sorted(
        o for o in ctx.funcoes
        if o.schema == gatilho["funcao_schema"] and o.nome == gatilho["funcao_nome"]
    )
    sem_argumentos = [o for o in candidatas if not o.args]
    if len(sem_argumentos) == 1:
        return sem_argumentos[0]
    return candidatas[0] if len(candidatas) == 1 else None
