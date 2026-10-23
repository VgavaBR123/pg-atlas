"""Etapa `relacionamentos nome`: candidatos de relacionamento inferidos pelo nome (decisão D-017).

Offline: lê `<doc>/catalogo/estrutura/*.json` (já mascarado), o `relacionamentos/grafo.json`
(precisa ter as arestas `fk_declarada`) e, de `config/fatos.yml`, só a chave `tenant.coluna`
(`tenant.valores` nunca é usado). Reescreve só as arestas com `fonte == "inferencia_nome"`.

Coluna `id_<x>` ou `<x>_id` aponta para a PK de uma coluna só, de tipo exatamente igual, de uma
tabela cujo nome é `x` (regra `exato`) ou termina em `_x` (regra `modulo`). A regra `qualificador`
é só reserva: usa o maior prefixo de tokens de `x` quando não há candidato pelas outras duas.
O grafo guarda só metadado, nunca valores de dado.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from typing import Any, NamedTuple

import yaml

from pg_atlas.cli import adicionar_argumentos_comuns
from pg_atlas.config import carregar_projeto
from pg_atlas.json_util import escrever_json
from pg_atlas.relacionamentos import (
    FONTE_FK,
    SAIDA,
    ErroRelacionamentos,
    carregar_estruturas,
    ler_grafo_existente,
    montar_grafo,
    validar,
)

FONTE_NOME = "inferencia_nome"
REGRAS = ("exato", "modulo", "qualificador")
PESO = {"exato": 1.0, "modulo": 0.8, "qualificador": 0.5}
BONUS_MESMO_SCHEMA = 0.2
TETO_DESTINOS = 20
TIPOS_ORIGEM = ("tabela", "particionada")

Relacao = tuple[str, str]
Coluna = tuple[str, str, str]  # schema, relacao, coluna


class Destino(NamedTuple):
    schema: str
    relacao: str
    coluna: str
    tipo: str


class Candidato(NamedTuple):
    destino: Destino
    regra: str
    pontuacao: float


# ----------------------------------------------------------------- entrada


def coluna_de_tenant(doc: Path) -> str | None:
    """Lê só `tenant.coluna` de `config/fatos.yml`; sem a chave, nada é excluído."""
    caminho = doc / "config" / "fatos.yml"
    if not caminho.is_file():
        return None
    try:
        dados = yaml.safe_load(caminho.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        raise ErroRelacionamentos(f"YAML inválido em {caminho}") from None
    tenant = dados.get("tenant") if isinstance(dados, dict) else None
    coluna = tenant.get("coluna") if isinstance(tenant, dict) else None
    return coluna if isinstance(coluna, str) and coluna else None


def extrair_x(nome: str) -> str | None:
    """`x` de `id_<x>` ou `<x>_id` (não vazio); prefixo `id_` tem prioridade."""
    if nome.startswith("id_") and len(nome) > 3:
        return nome[3:]
    if nome.endswith("_id") and len(nome) > 3:
        return nome[:-3]
    return None


def _pk_de_uma_coluna(rel: dict[str, Any]) -> str | None:
    for r in rel.get("restricoes", []):
        if r.get("tipo") == "pk" and len(r.get("colunas", [])) == 1:
            return r["colunas"][0]
    return None


def _eh_particao(rel: dict[str, Any]) -> bool:
    return rel.get("particao") is not None


def coletar_destinos(estruturas: dict[str, dict[str, Any]]) -> list[Destino]:
    """Relações não partição com PK de uma coluna só, com o tipo dessa coluna."""
    destinos: list[Destino] = []
    for schema in sorted(estruturas):
        for rel in estruturas[schema].get("relacoes", []):
            if rel.get("tipo") not in TIPOS_ORIGEM or _eh_particao(rel):
                continue
            pk = _pk_de_uma_coluna(rel)
            tipos = {c["nome"]: c.get("tipo") for c in rel.get("colunas", [])}
            if pk is not None and tipos.get(pk):
                destinos.append(Destino(schema, rel["nome"], pk, tipos[pk]))
    return destinos


class Indice(NamedTuple):
    exato: dict[tuple[str, str], list[Destino]]
    modulo: dict[tuple[str, str], list[Destino]]


def montar_indice(destinos: list[Destino]) -> Indice:
    """Indexa por (nome, tipo): `exato` pelo nome sem `_` iniciais, `modulo` por cada sufixo após `_`."""
    exato: dict[tuple[str, str], list[Destino]] = defaultdict(list)
    modulo: dict[tuple[str, str], list[Destino]] = defaultdict(list)
    for d in destinos:
        exato[(d.relacao.lstrip("_"), d.tipo)].append(d)
        for i, c in enumerate(d.relacao):
            if c == "_" and i + 1 < len(d.relacao):
                modulo[(d.relacao[i + 1 :], d.tipo)].append(d)
    return Indice(exato, modulo)


# ----------------------------------------------------------------- regras


def pontuar(regra: str, destino: Destino, schema_origem: str) -> float:
    base = PESO[regra] + (BONUS_MESMO_SCHEMA if destino.schema == schema_origem else 0.0)
    return round(min(base, 1.0), 2)


def _por_regras(x: str, tipo: str, indice: Indice, schema_origem: str, regra_q: str | None) -> list[Candidato]:
    """Melhor regra por destino para o nome `x` (`exato` vence `modulo`)."""
    melhor: dict[tuple[str, str], Candidato] = {}
    for regra, mapa in (("exato", indice.exato), ("modulo", indice.modulo)):
        for d in mapa.get((x, tipo), []):
            chave = (d.schema, d.relacao)
            if chave not in melhor:
                melhor[chave] = Candidato(d, regra_q or regra, pontuar(regra_q or regra, d, schema_origem))
    return list(melhor.values())


def candidatos(x: str, tipo: str, indice: Indice, schema_origem: str) -> list[Candidato]:
    """Candidatos de maior pontuação (com empates), ordenados por destino. Vazio se não há nenhum."""
    achados = _por_regras(x, tipo, indice, schema_origem, None)
    if not achados:
        tokens = x.split("_")
        for k in range(len(tokens) - 1, 0, -1):
            prefixo = "_".join(tokens[:k])
            if prefixo:
                achados = _por_regras(prefixo, tipo, indice, schema_origem, "qualificador")
                if achados:
                    break
    if not achados:
        return []
    topo = max(c.pontuacao for c in achados)
    return sorted((c for c in achados if c.pontuacao == topo), key=lambda c: (c.destino.schema, c.destino.relacao))


# ----------------------------------------------------------------- arestas


def montar_aresta(schema: str, relacao: str, coluna: str, cand: Candidato, total: int) -> dict[str, Any]:
    d = cand.destino
    return {
        "id": f"nome:{schema}.{relacao}.{coluna}->{d.schema}.{d.relacao}",
        "origem": {"schema": schema, "relacao": relacao, "colunas": [coluna]},
        "destino": {"schema": d.schema, "relacao": d.relacao, "colunas": [d.coluna]},
        "confianca": "hipotese",
        "fonte": FONTE_NOME,
        "taxa_casamento": None,
        "nota": f"ambíguo: {total} destinos possíveis" if total > 1 else None,
        "evidencia": [
            {"tipo": FONTE_NOME, "regra": cand.regra, "pontuacao": cand.pontuacao, "candidatos": total}
        ],
    }


def origens_de_fk(arestas: list[dict[str, Any]]) -> dict[Coluna, set[Relacao]]:
    """Colunas de origem de FK declarada, com os destinos (schema, relação) de cada uma."""
    mapa: dict[Coluna, set[Relacao]] = defaultdict(set)
    for a in arestas:
        if a["fonte"] != FONTE_FK:
            continue
        o, d = a["origem"], a["destino"]
        for c in o["colunas"]:
            mapa[(o["schema"], o["relacao"], c)].add((d["schema"], d["relacao"]))
    return mapa


def _zeros_calibracao() -> dict[str, dict[str, int]]:
    return {r: {"acerto_unico": 0, "acerto_ambiguo": 0, "erro": 0, "repetem_fk": 0} for r in REGRAS}


def _calibrar(cands: list[Candidato], fk_destinos: set[Relacao], calib: dict[str, dict[str, int]]) -> bool:
    """Conta o caso da coluna com FK; devolve False se não houve candidato (`sem_candidato`)."""
    if not cands:
        return False
    acertos = [c for c in cands if (c.destino.schema, c.destino.relacao) in fk_destinos]
    if not acertos:
        caso, regra = "erro", cands[0].regra
    else:
        caso = "acerto_unico" if len(cands) == 1 else "acerto_ambiguo"
        regra = acertos[0].regra
    calib[regra][caso] += 1
    if acertos:
        calib[regra]["repetem_fk"] += 1
    return True


def inferir(
    estruturas: dict[str, dict[str, Any]], arestas_fk: list[dict[str, Any]], tenant: str | None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Arestas `inferencia_nome` (ordenadas por `id`) e o bloco `resumo.inferencia_nome`."""
    indice = montar_indice(coletar_destinos(estruturas))
    fk_origens = origens_de_fk(arestas_fk)
    arestas: list[dict[str, Any]] = []
    calib = _zeros_calibracao()
    por_regra = {r: 0 for r in REGRAS}
    cont = {"analisadas": 0, "com_candidato": 0, "ambiguas": 0, "excl_fk": 0, "excl_tenant": 0, "sem_cand_fk": 0}
    for schema in sorted(estruturas):
        for rel in estruturas[schema].get("relacoes", []):
            if rel.get("tipo") not in TIPOS_ORIGEM or _eh_particao(rel):
                continue
            for col in rel.get("colunas", []):
                x = extrair_x(col["nome"])
                if x is None or not col.get("tipo"):
                    continue
                cands = candidatos(x, col["tipo"], indice, schema)
                chave = (schema, rel["nome"], col["nome"])
                if chave in fk_origens:  # a FK é a verdade: só calibra, sem aresta
                    cont["excl_fk"] += 1
                    if not _calibrar(cands, fk_origens[chave], calib):
                        cont["sem_cand_fk"] += 1
                    continue
                if tenant is not None and col["nome"] == tenant:
                    cont["excl_tenant"] += 1
                    continue
                cont["analisadas"] += 1
                if not cands:
                    continue
                cont["com_candidato"] += 1
                cont["ambiguas"] += len(cands) > 1
                novas = [montar_aresta(schema, rel["nome"], col["nome"], c, len(cands)) for c in cands]
                for a in sorted(novas, key=lambda a: a["id"])[:TETO_DESTINOS]:
                    arestas.append(a)
                    por_regra[a["evidencia"][0]["regra"]] += 1
    bloco = {
        "colunas_analisadas": cont["analisadas"],
        "colunas_com_candidato": cont["com_candidato"],
        "colunas_ambiguas": cont["ambiguas"],
        "arestas": len(arestas),
        "por_regra": por_regra,
        "excluidas_fk": cont["excl_fk"],
        "excluidas_tenant": cont["excl_tenant"],
        "calibracao_fk": {"por_regra": calib, "sem_candidato": cont["sem_cand_fk"]},
    }
    return sorted(arestas, key=lambda a: a["id"]), bloco


# ----------------------------------------------------------------- execução


def executar_nome(doc: Path) -> dict[str, Any]:
    estruturas = carregar_estruturas(doc / "catalogo")
    caminho = doc.joinpath(*SAIDA)
    if not caminho.is_file():
        raise ErroRelacionamentos(f"{caminho} não existe. Rode 'relacionamentos fks' antes.")
    existente = ler_grafo_existente(caminho)
    fks = [a for a in existente["arestas"] if a["fonte"] == FONTE_FK]
    if not fks:
        raise ErroRelacionamentos("O grafo não tem arestas fk_declarada. Rode 'relacionamentos fks' antes.")
    novas, bloco = inferir(estruturas, fks, coluna_de_tenant(doc))
    outras = [a for a in existente["arestas"] if a["fonte"] != FONTE_NOME]
    grafo = montar_grafo(outras, novas, bloco, existente["resumo"].get("inferencia_codigo"))
    validar(grafo)
    escrever_json(caminho, grafo)
    return grafo


def registrar(etapas: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    nome = etapas.add_parser("nome", help="candidatos inferidos pelo nome da coluna, confiança 'hipotese' (offline)")
    adicionar_argumentos_comuns(nome)
    nome.set_defaults(func=executar)


def executar(args: argparse.Namespace) -> int:
    projeto = carregar_projeto(args.doc, args.workspace)
    grafo = executar_nome(projeto.doc)
    b = grafo["resumo"]["inferencia_nome"]
    print(f"Grafo: {grafo['resumo']['total']} arestas ({SAIDA[0]}/{SAIDA[1]}); {FONTE_NOME}={b['arestas']}.")
    print(
        f"Colunas: analisadas={b['colunas_analisadas']}, com candidato={b['colunas_com_candidato']}, "
        f"ambíguas={b['colunas_ambiguas']}; excluídas: FK={b['excluidas_fk']}, tenant={b['excluidas_tenant']}."
    )
    print("Arestas por regra: " + ", ".join(f"{r}={n}" for r, n in b["por_regra"].items()) + ".")
    cal = b["calibracao_fk"]
    for r, c in cal["por_regra"].items():
        print(
            f"Calibração {r}: acerto único={c['acerto_unico']}, acerto ambíguo={c['acerto_ambiguo']}, "
            f"erro={c['erro']}, repetem FK={c['repetem_fk']}."
        )
    print(f"Calibração: sem candidato={cal['sem_candidato']}.")
    return 0
