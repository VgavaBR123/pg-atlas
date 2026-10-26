"""Comando `contexto lotes`: pacote de contexto por lote para a fase 3 (decisão D-023).

Offline e determinístico, sem LLM e sem conexão ao banco. Lê só arquivos locais do repositório de
documentação: `relacionamentos/lotes.json`, `relacionamentos/grafo.json`, `catalogo/estrutura/`,
`catalogo/estatisticas/`, `config/fatos.yml` (opcional) e `config/dominios.yml`.

Grava `_controle/lotes/<id>.json` (um por lote) e `_controle/lotes/contexto-indice.json`. Os arquivos
`relacionamentos-*` da mesma pasta (analista de relacionamentos, D-021) nunca são tocados.

Regras de segurança:
- das estatísticas de coluna só passam `null_frac`, `n_distinct`, `avg_width` e `correlation`
  (nunca `most_common_vals`, `histogram_bounds` e semelhantes); o esquema JSON recusa qualquer outra chave;
- literais em definições de view, checks, predicados, padrões e limites de partição são neutralizados
  (`sanitizar_codigo`: strings viram `'?'`, números com mais de 4 dígitos viram `0`);
- `config/fatos.yml`: `tenant.valores` nunca é lido (usa `ler_fatos`);
- valores das tabelas de domínio aprovadas NÃO são lidos aqui (o comando não conecta). O pacote só lista
  as tabelas de domínio do lote com `situacao: "pendente"`; a leitura dos valores, pelo `pg_atlas` com
  guarda de SQL e LIMIT, é tarefa posterior.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from pg_atlas.cli import adicionar_argumentos_comuns
from pg_atlas.config import carregar_projeto
from pg_atlas.json_util import escrever_json, serializar_json
from pg_atlas.relacionamentos import SAIDA, ErroRelacionamentos, carregar_estruturas, ler_grafo_existente
from pg_atlas.relacionamentos_codigo import FONTE_CODIGO
from pg_atlas.relacionamentos_lotes import PASTA_LOTES, _cita, ler_fatos, sanitizar_codigo
from pg_atlas.verificar_amostragem import linhas_estimadas, mapa_de_particoes

VERSAO_FORMATO = 1
ARQ_INDICE = "contexto-indice.json"
ARQ_LOTES = ("relacionamentos", "lotes.json")
PADRAO_ARQUIVO = re.compile(r"^(lote|leve-vazia|leve-temporaria)-\d{3,}\.json$")
ESTATISTICAS_PERMITIDAS = ("avg_width", "correlation", "n_distinct", "null_frac")
CONFIANCAS = ("declarado", "confirmado", "hipotese")

# Limites de tamanho (D-023). Só os pacotes de lote do grafo são cortados.
MAX_BYTES = 250_000  # alvo por pacote; acima disso aplicam-se os cortes na ordem de CORTES
MAX_COMENTARIO = 600  # caracteres por comentário (sempre aplicado)
MAX_DEFINICAO = 6_000  # caracteres por definição de view/check (sempre aplicado)
MAX_HIPOTESES = 150  # arestas `hipotese` por pacote (sempre aplicado; ordem: maior taxa, depois id)
MAX_EXTERNAS = 200  # arestas declarado/confirmado com uma ponta fora do lote, por pacote (sempre aplicado)
MAX_EXTERNAS_CORTE = 40  # teto do corte de tamanho
MAX_USOS = 100  # views/rotinas listadas por pacote

Chave = tuple[str, str]


# ----------------------------------------------------------------- leitura

@dataclass
class Fontes:
    """Tudo o que os pacotes leem, já carregado."""

    lotes: dict[str, Any]
    relacoes: dict[Chave, dict[str, Any]]
    estatisticas: dict[Chave, dict[str, Any]]
    linhas: dict[Chave, int | None]
    arestas: list[dict[str, Any]]
    fatos: list[str]
    dominios: set[str]


def _ler_json(caminho: Path) -> Any:
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ErroRelacionamentos(f"Não foi possível ler {caminho}. Rode as etapas anteriores.") from None


def ler_estatisticas_colunas(raiz: Path) -> dict[Chave, dict[str, Any]]:
    """Por relação: tamanho, linhas e estatísticas permitidas por coluna (prefere a entrada não herdada)."""
    pasta = raiz / "estatisticas"
    saida: dict[Chave, dict[str, Any]] = {}
    for arquivo in sorted(pasta.glob("*.json")) if pasta.is_dir() else []:
        dados = _ler_json(arquivo)
        for rel in dados.get("relacoes", []) if isinstance(dados, dict) else []:
            colunas: dict[str, dict[str, Any]] = {}
            for c in sorted(rel.get("colunas", []), key=lambda x: (x["nome"], bool(x.get("herdada")))):
                if c["nome"] not in colunas or (colunas[c["nome"]]["herdada"] and not c.get("herdada")):
                    colunas[c["nome"]] = {"herdada": bool(c.get("herdada")), **{k: c.get(k) for k in ESTATISTICAS_PERMITIDAS}}
            saida[(arquivo.stem, rel["nome"])] = {
                "paginas": rel.get("paginas"),
                "linhas_estimadas": rel.get("linhas_estimadas"),
                "tamanho_total_bytes": rel.get("tamanho_total_bytes"),
                "colunas": colunas,
            }
    return saida


def ler_dominios_aprovados(doc: Path) -> set[str]:
    """Nomes `schema.tabela` de `config/dominios.yml` (lista `aprovadas`). Arquivo ausente: conjunto vazio."""
    caminho = doc / "config" / "dominios.yml"
    if not caminho.is_file():
        return set()
    try:
        dados = yaml.safe_load(caminho.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        raise ErroRelacionamentos(f"YAML inválido em {caminho}") from None
    itens = dados.get("aprovadas") if isinstance(dados, dict) else None
    return {str(i) for i in itens or [] if isinstance(i, str)}


def carregar_fontes(doc: Path) -> Fontes:
    lotes = _ler_json(doc.joinpath(*ARQ_LOTES))
    caminho_grafo = doc.joinpath(*SAIDA)
    if not caminho_grafo.is_file():
        raise ErroRelacionamentos(f"{caminho_grafo} não existe. Rode 'relacionamentos fks' antes.")
    grafo = ler_grafo_existente(caminho_grafo)
    raiz = doc / "catalogo"
    estruturas = carregar_estruturas(raiz)
    relacoes = {(s, r["nome"]): r for s, d in estruturas.items() for r in d.get("relacoes", [])}
    estat = ler_estatisticas_colunas(raiz)
    filhos = mapa_de_particoes(estruturas)
    linhas = {k: linhas_estimadas(k, k in filhos, estat, filhos) for k in relacoes}
    return Fontes(lotes, relacoes, estat, linhas, grafo["arestas"], ler_fatos(doc), ler_dominios_aprovados(doc))


# ----------------------------------------------------------------- conteúdo de uma tabela


def _cortar(texto: Any, limite: int) -> str | None:
    if not isinstance(texto, str):
        return None
    return texto if len(texto) <= limite else texto[:limite] + "…[cortado]"


def _sanitizada(texto: Any, limite: int = MAX_DEFINICAO) -> str | None:
    return _cortar(sanitizar_codigo(texto), limite) if isinstance(texto, str) else None


def _coluna(c: dict[str, Any], stats: dict[str, Any] | None) -> dict[str, Any]:
    saida: dict[str, Any] = {
        "ordem": c.get("ordem"),
        "nome": c["nome"],
        "tipo": c.get("tipo"),
        "obrigatoria": bool(c.get("obrigatoria")),
        "padrao": _sanitizada(c.get("padrao"), 300),
        "comentario": _cortar(c.get("comentario"), MAX_COMENTARIO),
        "gerada": bool(c.get("gerada")) or None,
        "identidade": c.get("identidade"),
    }
    if stats:
        saida["estatisticas"] = {k: stats.get(k) for k in ESTATISTICAS_PERMITIDAS} | {"herdada": stats["herdada"]}
    return saida


def _restricoes(rel: dict[str, Any], tipo: str) -> list[dict[str, Any]]:
    return sorted(
        (r for r in rel.get("restricoes", []) if r.get("tipo") == tipo), key=lambda r: r["nome"]
    )


def _gatilhos(rel: dict[str, Any]) -> list[dict[str, Any]]:
    itens = [
        {
            "nome": g["nome"],
            "momento": g.get("momento"),
            "eventos": sorted(g.get("eventos", [])),
            "funcao": f"{g.get('funcao_schema')}.{g.get('funcao_nome')}",
        }
        for g in rel.get("gatilhos", [])
    ]
    return sorted(itens, key=lambda g: g["nome"])


def _particao(rel: dict[str, Any]) -> dict[str, Any] | None:
    p = rel.get("particao")
    if not isinstance(p, dict):
        return None
    return {"pai": f"{p.get('pai_schema')}.{p.get('pai_nome')}", "limite": _sanitizada(p.get("limite"), 300)}


def montar_tabela(item: dict[str, Any], fontes: Fontes) -> dict[str, Any]:
    """Estrutura completa de uma tabela de lote do grafo (sem valores de dado)."""
    chave = (item["schema"], item["nome"])
    rel = fontes.relacoes.get(chave, {})
    est = fontes.estatisticas.get(chave, {})
    por_coluna = est.get("colunas", {})
    pk = next((r["colunas"] for r in _restricoes(rel, "pk")), [])
    return {
        "schema": item["schema"],
        "nome": item["nome"],
        "classe": item["classe"],
        "particao_de": item.get("particao_de"),
        "tipo": rel.get("tipo"),
        "comentario": _cortar(rel.get("comentario"), MAX_COMENTARIO),
        "linhas_estimadas": fontes.linhas.get(chave),
        "tamanho_total_bytes": est.get("tamanho_total_bytes"),
        "colunas": [
            _coluna(c, por_coluna.get(c["nome"])) for c in sorted(rel.get("colunas", []), key=lambda x: x.get("ordem") or 0)
        ],
        "pk": list(pk),
        "unicas": [{"nome": r["nome"], "colunas": list(r["colunas"])} for r in _restricoes(rel, "unique")],
        "checks": [
            {"nome": r["nome"], "colunas": list(r["colunas"]), "definicao": _sanitizada(r.get("definicao"))}
            for r in _restricoes(rel, "check")
        ],
        "indices": sorted(
            (
                {
                    "nome": i["nome"],
                    "colunas": list(i.get("colunas", [])),
                    "unico": bool(i.get("unico")),
                    "primario": bool(i.get("primario")),
                    "metodo": i.get("metodo"),
                    "predicado": _sanitizada(i.get("predicado"), 300),
                }
                for i in rel.get("indices", [])
            ),
            key=lambda i: i["nome"],
        ),
        "fks_declaradas": [
            {
                "nome": r["nome"],
                "colunas": list(r["colunas"]),
                "referencia": f"{r.get('ref_schema')}.{r.get('ref_relacao')}",
                "ref_colunas": list(r.get("ref_colunas", [])),
                "validada": bool(r.get("validada", True)),
            }
            for r in _restricoes(rel, "fk")
        ],
        "gatilhos": _gatilhos(rel),
        "particao": _particao(rel),
        "definicao": _sanitizada(rel.get("definicao")),
        "depende_de": sorted(f"{d['schema']}.{d['relacao']}" for d in rel.get("depende_de", [])),
    }


def montar_tabela_leve(item: dict[str, Any], fontes: Fontes) -> dict[str, Any]:
    """Pacote reduzido: nome, schema, classe, contagem de colunas e comentário."""
    rel = fontes.relacoes.get((item["schema"], item["nome"]), {})
    return {
        "schema": item["schema"],
        "nome": item["nome"],
        "classe": item["classe"],
        "tipo": rel.get("tipo"),
        "total_colunas": len(rel.get("colunas", [])),
        "comentario": _cortar(rel.get("comentario"), MAX_COMENTARIO),
    }


# ----------------------------------------------------------------- relacionamentos, usos, fatos, domínios


def _nome(s: dict[str, Any]) -> str:
    return f"{s['schema']}.{s['relacao']}"


def _aresta(a: dict[str, Any], dentro: set[str]) -> dict[str, Any]:
    am = next((e for e in reversed(a["evidencia"]) if e.get("tipo") == "amostragem"), None)
    an = next((e for e in reversed(a["evidencia"]) if e.get("tipo") == "analise"), None)
    o, d = _nome(a["origem"]), _nome(a["destino"])
    return {
        "id": a["id"],
        "origem": {"relacao": o, "colunas": list(a["origem"]["colunas"])},
        "destino": {"relacao": d, "colunas": list(a["destino"]["colunas"])},
        "posicao": "interna" if o in dentro and d in dentro else ("saida" if o in dentro else "entrada"),
        "confianca": a["confianca"],
        "fonte": a["fonte"],
        "taxa_casamento": a.get("taxa_casamento"),
        "amostrados": am.get("amostrados") if am else None,
        "nota": a.get("nota"),
        "analise": {"resultado": an.get("resultado"), "lote": an.get("lote"), "justificativa": an.get("justificativa")}
        if an
        else None,
    }


def _chave_hipotese(a: dict[str, Any]) -> tuple[float, str]:
    taxa = a["taxa_casamento"]
    return (-(taxa if taxa is not None else -1.0), a["id"])


def selecionar_arestas(
    arestas: Iterable[dict[str, Any]],
    dentro: set[str],
    max_hipoteses: int = MAX_HIPOTESES,
    max_externas: int = MAX_EXTERNAS,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Arestas do grafo com pelo menos uma ponta no lote, com tetos (ordem determinística).

    Arestas internas declaradas/confirmadas nunca são cortadas. As externas declaradas/confirmadas
    (declarado antes de confirmado, depois `id`) e as `hipotese` (maior taxa, depois `id`) têm teto.
    Devolve a lista e as omissões por tipo.
    """
    tocam = [a for a in arestas if _nome(a["origem"]) in dentro or _nome(a["destino"]) in dentro]
    firmes = [a for a in tocam if a["confianca"] != "hipotese"]
    internas = [a for a in firmes if _nome(a["origem"]) in dentro and _nome(a["destino"]) in dentro]
    externas = sorted(
        (a for a in firmes if a not in internas), key=lambda a: (a["confianca"] != "declarado", a["id"])
    )
    hipoteses = sorted((a for a in tocam if a["confianca"] == "hipotese"), key=_chave_hipotese)
    escolhidas = [*internas, *externas[:max_externas], *hipoteses[:max_hipoteses]]
    omitidas = {
        "externas_omitidas_pelo_teto": max(0, len(externas) - max_externas),
        "hipoteses_omitidas_pelo_teto": max(0, len(hipoteses) - max_hipoteses),
    }
    return [_aresta(a, dentro) for a in sorted(escolhidas, key=lambda a: a["id"])], {k: v for k, v in omitidas.items() if v}


def usos_no_codigo(
    tabelas: list[dict[str, Any]], arestas: list[dict[str, Any]], dentro: set[str], fontes: Fontes
) -> dict[str, list[dict[str, str]]]:
    """Views, materializadas e rotinas que usam as tabelas do lote (só nomes).

    Views e materializadas: `depende_de` do catálogo. Rotinas: gatilhos das tabelas e objetos de código
    citados nas arestas `inferencia_codigo` do grafo que tocam o lote. Rotinas sem aresta não aparecem
    (o levantamento completo é da fase 4).
    """
    views: dict[str, dict[str, str]] = {}
    for (schema, nome), rel in fontes.relacoes.items():
        if rel.get("tipo") in ("view", "materializada") and any(
            f"{d['schema']}.{d['relacao']}" in dentro for d in rel.get("depende_de", [])
        ):
            views[f"{schema}.{nome}"] = {"nome": f"{schema}.{nome}", "tipo": rel["tipo"]}
    rotinas: dict[str, dict[str, str]] = {}
    for t in tabelas:
        for g in t.get("gatilhos", []):
            rotinas[g["funcao"]] = {"nome": g["funcao"], "tipo": "function", "motivo": "gatilho"}
    for a in arestas:
        if a["fonte"] != FONTE_CODIGO:
            continue
        for e in a["evidencia"]:
            for o in e.get("objetos", []) if e.get("tipo") == FONTE_CODIGO else []:
                nome = f"{o['schema']}.{o['nome']}"
                if o["tipo"] in ("view", "materializada"):
                    views.setdefault(nome, {"nome": nome, "tipo": o["tipo"]})
                else:
                    rotinas.setdefault(nome, {"nome": nome, "tipo": o["tipo"], "motivo": "codigo"})
    return {
        "views": [views[k] for k in sorted(views)][:MAX_USOS],
        "rotinas": [rotinas[k] for k in sorted(rotinas)][:MAX_USOS],
    }


def fatos_do_lote(dentro: set[str], fontes: Fontes) -> list[str]:
    """Fatos de `fatos.yml` que citam uma tabela do lote (`schema.tabela` ou nome da tabela)."""
    nomes = sorted(dentro | {n.split(".", 1)[1] for n in dentro})
    return [f for f in fontes.fatos if any(_cita(f, n) for n in nomes)]


def dominios_do_lote(dentro: set[str], aprovados: set[str]) -> list[dict[str, Any]]:
    """Tabelas de domínio aprovadas do lote. Os valores não são lidos aqui: `situacao` fica `pendente`."""
    return [{"tabela": n, "situacao": "pendente", "valores": None} for n in sorted(dentro & aprovados)]


# ----------------------------------------------------------------- pacote


def _cabecalho(lote: dict[str, Any]) -> dict[str, Any]:
    return {
        "formato": "contexto_lote",
        "versao_formato": VERSAO_FORMATO,
        "lote": lote["id"],
        "tipo": lote["tipo"],
        "classe_leve": lote.get("classe_leve"),
        "schemas": sorted(lote["schemas"]),
    }


def montar_pacote_leve(lote: dict[str, Any], fontes: Fontes) -> dict[str, Any]:
    tabelas = [montar_tabela_leve(t, fontes) for t in lote["tabelas"]]
    pacote = _cabecalho(lote)
    pacote.update({"total_tabelas": len(tabelas), "tabelas": tabelas, "truncamento": {}})
    return pacote


def _cortes(pacote: dict[str, Any]) -> list[tuple[str, Any]]:
    """Cortes de tamanho, na ordem em que são aplicados se o pacote passar de `MAX_BYTES`."""

    def sem_definicoes(p: dict[str, Any]) -> None:
        for t in p["tabelas"]:
            t["checks"] = [{**c, "definicao": None} for c in t["checks"]]
            t["definicao"] = None

    def sem_hipoteses(p: dict[str, Any]) -> None:
        p["relacionamentos"] = [a for a in p["relacionamentos"] if a["confianca"] != "hipotese"]

    def poucas_externas(p: dict[str, Any]) -> None:
        externas = [a for a in p["relacionamentos"] if a["posicao"] != "interna" and a["confianca"] != "hipotese"]
        remover = {a["id"] for a in externas[MAX_EXTERNAS_CORTE:]}
        p["relacionamentos"] = [a for a in p["relacionamentos"] if a["id"] not in remover]

    def sem_comentarios_de_coluna(p: dict[str, Any]) -> None:
        for t in p["tabelas"]:
            for c in t["colunas"]:
                c["comentario"] = _cortar(c["comentario"], 120)

    return [
        ("definicoes_omitidas", sem_definicoes),
        ("hipoteses_omitidas_por_tamanho", sem_hipoteses),
        ("externas_cortadas_por_tamanho", poucas_externas),
        ("comentarios_de_coluna_cortados", sem_comentarios_de_coluna),
    ]


def aplicar_cortes(pacote: dict[str, Any], max_bytes: int = MAX_BYTES) -> dict[str, Any]:
    """Aplica os cortes em ordem até o pacote caber; registra em `truncamento` o que foi feito."""
    for nome, corte in _cortes(pacote):
        if tamanho(pacote) <= max_bytes:
            break
        corte(pacote)
        pacote["truncamento"][nome] = True
    return pacote


def montar_pacote_grafo(lote: dict[str, Any], fontes: Fontes) -> dict[str, Any]:
    dentro = {f"{t['schema']}.{t['nome']}" for t in lote["tabelas"]}
    tabelas = [montar_tabela(t, fontes) for t in lote["tabelas"]]
    relacionamentos, omitidas = selecionar_arestas(fontes.arestas, dentro)
    tocam = [a for a in fontes.arestas if a["fonte"] == FONTE_CODIGO and (_nome(a["origem"]) in dentro or _nome(a["destino"]) in dentro)]
    pacote = _cabecalho(lote)
    pacote.update(
        {
            "total_tabelas": len(tabelas),
            "tabelas": tabelas,
            "relacionamentos": relacionamentos,
            "usos": usos_no_codigo(tabelas, tocam, dentro, fontes),
            "fatos": fatos_do_lote(dentro, fontes),
            "dominios": dominios_do_lote(dentro, fontes.dominios),
            "truncamento": dict(omitidas),
        }
    )
    return aplicar_cortes(pacote)


def montar_pacote(lote: dict[str, Any], fontes: Fontes) -> dict[str, Any]:
    return montar_pacote_leve(lote, fontes) if lote["tipo"] == "leve" else montar_pacote_grafo(lote, fontes)


def tamanho(obj: Any) -> int:
    return len(serializar_json(obj).encode("utf-8"))


# ----------------------------------------------------------------- validação e execução


def _esquema(nome: str) -> dict[str, Any]:
    return json.loads((resources.files("pg_atlas.esquemas") / nome).read_text(encoding="utf-8"))


def validar(documento: Any, esquema: str, rotulo: str) -> None:
    erros = sorted(
        jsonschema.Draft202012Validator(_esquema(esquema)).iter_errors(documento),
        key=lambda e: [str(p) for p in e.path],
    )
    if erros:
        onde = "/".join(str(p) for p in erros[0].absolute_path) or "(raiz)"
        raise ErroRelacionamentos(f"{rotulo} fora do formato em {onde}: {erros[0].message[:200]} ({len(erros)} problema(s)).")


def conferir_cobertura(lotes: list[dict[str, Any]]) -> None:
    """Toda tabela de `lotes.json` está em exatamente um lote."""
    vezes: dict[str, int] = {}
    for lote in lotes:
        for t in lote["tabelas"]:
            n = f"{t['schema']}.{t['nome']}"
            vezes[n] = vezes.get(n, 0) + 1
    repetidas = sorted(n for n, v in vezes.items() if v != 1)
    if repetidas:
        raise ErroRelacionamentos(f"{len(repetidas)} tabela(s) fora de exatamente um lote em lotes.json; nada foi gravado.")


def gerar(doc: Path) -> dict[str, Any]:
    """Grava os pacotes e o índice; devolve o índice. Pacotes de lotes que sumiram são removidos."""
    fontes = carregar_fontes(doc)
    validar_lotes = fontes.lotes.get("lotes")
    if not isinstance(validar_lotes, list) or not validar_lotes:
        raise ErroRelacionamentos("relacionamentos/lotes.json sem lotes. Rode 'relacionamentos agrupar'.")
    conferir_cobertura(validar_lotes)
    pasta = doc.joinpath(*PASTA_LOTES)
    pacotes = {l["id"]: montar_pacote(l, fontes) for l in sorted(validar_lotes, key=lambda x: x["id"])}
    for lote_id, pacote in pacotes.items():
        validar(pacote, "contexto-lote.schema.json", f"pacote {lote_id}")
    entradas = []
    for lote_id, pacote in pacotes.items():
        nome = f"{lote_id}.json"
        if not PADRAO_ARQUIVO.match(nome):
            raise ErroRelacionamentos(f"Id de lote fora do padrão esperado: {lote_id}.")
        escrever_json(pasta / nome, pacote)
        entradas.append(_entrada(lote_id, pacote))
    for arquivo in sorted(pasta.glob("*.json")):
        if PADRAO_ARQUIVO.match(arquivo.name) and arquivo.stem not in pacotes:
            arquivo.unlink()
    indice = montar_indice(entradas, fontes, validar_lotes)
    validar(indice, "contexto-indice.schema.json", "índice")
    escrever_json(pasta / ARQ_INDICE, indice)
    return indice


def _entrada(lote_id: str, pacote: dict[str, Any]) -> dict[str, Any]:
    return {
        "lote": lote_id,
        "arquivo": f"{lote_id}.json",
        "tipo": pacote["tipo"],
        "tabelas": pacote["total_tabelas"],
        "bytes": tamanho(pacote),
        "truncamento": sorted(pacote["truncamento"]),
        "dominios_pendentes": len(pacote.get("dominios", [])),
    }


def montar_indice(entradas: list[dict[str, Any]], fontes: Fontes, lotes: list[dict[str, Any]]) -> dict[str, Any]:
    maior = max(entradas, key=lambda e: (e["bytes"], e["lote"]))
    em_lote = {f"{t['schema']}.{t['nome']}" for l in lotes for t in l["tabelas"]}
    return {
        "formato": "contexto_indice",
        "versao_formato": VERSAO_FORMATO,
        "limites": {
            "max_bytes": MAX_BYTES,
            "max_comentario": MAX_COMENTARIO,
            "max_definicao": MAX_DEFINICAO,
            "max_externas": MAX_EXTERNAS,
            "max_externas_corte": MAX_EXTERNAS_CORTE,
            "max_hipoteses": MAX_HIPOTESES,
            "max_usos": MAX_USOS,
        },
        "lotes": sorted(entradas, key=lambda e: e["lote"]),
        "resumo": {
            "total_lotes": len(entradas),
            "total_tabelas": len(em_lote),
            "total_bytes": sum(e["bytes"] for e in entradas),
            "maior_pacote": {"lote": maior["lote"], "bytes": maior["bytes"]},
            "lotes_truncados": sum(bool(e["truncamento"]) for e in entradas),
            "dominios_pendentes": sum(e["dominios_pendentes"] for e in entradas),
            "dominios_aprovados_sem_lote": len(fontes.dominios - em_lote),
            "fatos_lidos": len(fontes.fatos),
        },
    }


# ----------------------------------------------------------------- CLI


def registrar(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("contexto", help="pacotes de contexto para a documentação (offline)")
    etapas = parser.add_subparsers(dest="etapa", metavar="etapa", required=True)
    p = etapas.add_parser("lotes", help="gera um pacote de contexto por lote de lotes.json (offline)")
    adicionar_argumentos_comuns(p)
    p.set_defaults(func=executar)


def executar(args: argparse.Namespace) -> int:
    projeto = carregar_projeto(args.doc, args.workspace)
    indice = gerar(projeto.doc)
    r = indice["resumo"]
    print(f"Pacotes: {r['total_lotes']} lotes, {r['total_tabelas']} tabelas, {r['total_bytes'] // 1024} KB no total.")
    print(f"Maior pacote: {r['maior_pacote']['lote']} com {r['maior_pacote']['bytes']} bytes (alvo {MAX_BYTES}).")
    print(f"Lotes truncados: {r['lotes_truncados']}. Tabelas de domínio com valores pendentes: {r['dominios_pendentes']}.")
    return 0
