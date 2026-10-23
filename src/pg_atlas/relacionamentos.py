"""Comando `relacionamentos <etapa>`: grafo de relacionamentos entre tabelas (decisão D-016).

Etapa `fks`: FKs declaradas, direto do catálogo, confiança `declarado`. Não conecta ao banco;
lê só `<doc>/catalogo/estrutura/*.json` (já mascarado) e grava `<doc>/relacionamentos/grafo.json`
(determinístico, validado por `esquemas/grafo.schema.json`).

O comando reescreve só as arestas com `fonte == "fk_declarada"`: as de outras fontes,
já presentes no arquivo, são preservadas. O grafo guarda só metadado, nunca valores de dado.
"""

from __future__ import annotations

import argparse
import json
from importlib import resources
from pathlib import Path
from typing import Any

import jsonschema

from pg_atlas.cli import adicionar_argumentos_comuns
from pg_atlas.config import carregar_projeto
from pg_atlas.erros import ErroAtlas
from pg_atlas.json_util import escrever_json

VERSAO_FORMATO = 1
FONTE_FK = "fk_declarada"
SAIDA = ("relacionamentos", "grafo.json")
NOTA_NOT_VALID = "FK declarada NOT VALID: o banco não garante as linhas anteriores à restrição"

Relacao = tuple[str, str]


class ErroRelacionamentos(ErroAtlas):
    """Catálogo ausente, grafo existente inválido ou conferência que falhou."""

    codigo_saida = 1


# ----------------------------------------------------------------- entrada


def carregar_estruturas(raiz: Path) -> dict[str, dict[str, Any]]:
    """Lê `estrutura/*.json` por schema; falha com mensagem clara se faltar o catálogo."""
    pasta = raiz / "estrutura"
    arquivos = sorted(pasta.glob("*.json")) if pasta.is_dir() else []
    if not arquivos:
        raise ErroRelacionamentos(f"Catálogo ausente: nenhum arquivo em {pasta}. Rode 'pg_atlas catalogo'.")
    estruturas: dict[str, dict[str, Any]] = {}
    for arquivo in arquivos:
        try:
            dados = json.loads(arquivo.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise ErroRelacionamentos(f"Não foi possível ler {arquivo}. Rode 'pg_atlas catalogo'.") from None
        if not isinstance(dados, dict):
            raise ErroRelacionamentos(f"{arquivo} deveria conter um objeto JSON.")
        estruturas[arquivo.stem] = dados
    return estruturas


# ----------------------------------------------------------------- arestas


def montar_aresta(schema: str, relacao: str, restricao: dict[str, Any]) -> dict[str, Any]:
    """Aresta `fk_declarada` de uma restrição do tipo `fk`."""
    validada = bool(restricao.get("validada", True))
    return {
        "id": f"fk:{schema}.{relacao}.{restricao['nome']}",
        "origem": {"schema": schema, "relacao": relacao, "colunas": list(restricao["colunas"])},
        "destino": {
            "schema": restricao["ref_schema"],
            "relacao": restricao["ref_relacao"],
            "colunas": list(restricao["ref_colunas"]),
        },
        "confianca": "declarado",
        "fonte": FONTE_FK,
        "taxa_casamento": None,
        "nota": None if validada else NOTA_NOT_VALID,
        "evidencia": [
            {
                "tipo": FONTE_FK,
                "restricao": restricao["nome"],
                "validada": validada,
                "acao_delete": restricao.get("acao_delete"),
                "acao_update": restricao.get("acao_update"),
            }
        ],
    }


def extrair_fks(estruturas: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], set[Relacao]]:
    """Arestas de todas as FKs do catálogo (ordenadas por `id`) e o conjunto de relações."""
    arestas: list[dict[str, Any]] = []
    relacoes: set[Relacao] = set()
    for schema, doc in estruturas.items():
        for rel in doc.get("relacoes", []):
            relacoes.add((schema, rel["nome"]))
            for r in rel.get("restricoes", []):
                if r.get("tipo") == "fk":
                    arestas.append(montar_aresta(schema, rel["nome"], r))
    return sorted(arestas, key=lambda a: a["id"]), relacoes


def conferir_arestas(arestas: list[dict[str, Any]], relacoes: set[Relacao], total_catalogo: int) -> None:
    """Conferências da D-016 (item 5). Mensagens só com caminho e nome de objeto."""
    ids = [a["id"] for a in arestas]
    repetidos = sorted({i for i in ids if ids.count(i) > 1}) if len(set(ids)) != len(ids) else []
    if repetidos:
        raise ErroRelacionamentos(f"id de aresta repetido ({len(repetidos)}): {repetidos[0]}")
    if len(arestas) != total_catalogo:
        raise ErroRelacionamentos(
            f"FKs no catálogo ({total_catalogo}) diferem das arestas {FONTE_FK} ({len(arestas)})."
        )
    for a in arestas:
        d = a["destino"]
        if (d["schema"], d["relacao"]) not in relacoes:
            raise ErroRelacionamentos(
                f"Destino fora do catálogo em {a['id']}: {d['schema']}.{d['relacao']}"
            )
        if len(a["origem"]["colunas"]) != len(d["colunas"]):
            raise ErroRelacionamentos(f"Número de colunas diferente entre origem e destino em {a['id']}")


def contar_fks_catalogo(estruturas: dict[str, dict[str, Any]]) -> int:
    return sum(
        1
        for doc in estruturas.values()
        for rel in doc.get("relacoes", [])
        for r in rel.get("restricoes", [])
        if r.get("tipo") == "fk"
    )


# ----------------------------------------------------------------- resumo


def _contar(valores: list[str]) -> dict[str, int]:
    return {v: valores.count(v) for v in sorted(set(valores))}


def montar_resumo(arestas: list[dict[str, Any]]) -> dict[str, Any]:
    """Resumo derivado das arestas."""
    fks = [a for a in arestas if a["fonte"] == FONTE_FK]
    return {
        "total": len(arestas),
        "por_confianca": _contar([a["confianca"] for a in arestas]),
        "por_fonte": _contar([a["fonte"] for a in arestas]),
        "fk_declarada": {
            "entre_schemas": sum(a["origem"]["schema"] != a["destino"]["schema"] for a in fks),
            "autorreferencias": sum(
                (a["origem"]["schema"], a["origem"]["relacao"]) == (a["destino"]["schema"], a["destino"]["relacao"])
                for a in fks
            ),
            "varias_colunas": sum(len(a["origem"]["colunas"]) > 1 for a in fks),
            "not_valid": sum(any(e.get("validada") is False for e in a["evidencia"]) for a in fks),
        },
    }


# ----------------------------------------------------------------- validação


def carregar_esquema() -> dict[str, Any]:
    texto = (resources.files("pg_atlas.esquemas") / "grafo.schema.json").read_text(encoding="utf-8")
    return json.loads(texto)


def validar(documento: Any, rotulo: str = "grafo.json") -> None:
    erros = sorted(
        jsonschema.Draft202012Validator(carregar_esquema()).iter_errors(documento),
        key=lambda e: [str(p) for p in e.path],
    )
    if erros:
        onde = "/".join(str(p) for p in erros[0].absolute_path) or "(raiz)"
        raise ErroRelacionamentos(
            f"{rotulo} fora do formato em {onde}: {erros[0].message[:200]} "
            f"({len(erros)} problema(s) no total)."
        )


def arestas_de_outras_fontes(caminho: Path) -> list[dict[str, Any]]:
    """Arestas já gravadas cuja fonte não é `fk_declarada`; grafo existente inválido falha."""
    if not caminho.is_file():
        return []
    try:
        existente = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ErroRelacionamentos(f"{caminho} existe mas não é um JSON válido; nada foi sobrescrito.") from None
    validar(existente, f"{caminho.name} existente")
    return [a for a in existente["arestas"] if a["fonte"] != FONTE_FK]


# ----------------------------------------------------------------- execução


def montar_grafo(fks: list[dict[str, Any]], outras: list[dict[str, Any]]) -> dict[str, Any]:
    arestas = sorted(fks + outras, key=lambda a: a["id"])
    ids = [a["id"] for a in arestas]
    if len(set(ids)) != len(ids):
        repetido = next(i for i in ids if ids.count(i) > 1)
        raise ErroRelacionamentos(f"id de aresta repetido: {repetido}")
    return {"versao_formato": VERSAO_FORMATO, "arestas": arestas, "resumo": montar_resumo(arestas)}


def executar_fks(doc: Path) -> dict[str, Any]:
    estruturas = carregar_estruturas(doc / "catalogo")
    fks, relacoes = extrair_fks(estruturas)
    conferir_arestas(fks, relacoes, contar_fks_catalogo(estruturas))
    caminho = doc.joinpath(*SAIDA)
    grafo = montar_grafo(fks, arestas_de_outras_fontes(caminho))
    validar(grafo)
    escrever_json(caminho, grafo)
    return grafo


def registrar(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "relacionamentos", help="grafo de relacionamentos entre tabelas (sem conectar ao banco, nas etapas offline)"
    )
    etapas = parser.add_subparsers(dest="etapa", metavar="etapa", required=True)
    fks = etapas.add_parser("fks", help="FKs declaradas do catálogo, confiança 'declarado'")
    adicionar_argumentos_comuns(fks)
    fks.set_defaults(func=executar)


def executar(args: argparse.Namespace) -> int:
    projeto = carregar_projeto(args.doc, args.workspace)
    grafo = executar_fks(projeto.doc)
    r = grafo["resumo"]
    f = r["fk_declarada"]
    print(f"Grafo: {r['total']} arestas ({SAIDA[0]}/{SAIDA[1]}); fk_declarada={r['por_fonte'].get(FONTE_FK, 0)}.")
    print(
        f"FKs: entre schemas={f['entre_schemas']}, autorreferências={f['autorreferencias']}, "
        f"várias colunas={f['varias_colunas']}, NOT VALID={f['not_valid']}."
    )
    return 0
