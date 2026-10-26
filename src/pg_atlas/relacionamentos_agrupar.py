"""Etapa `relacionamentos agrupar`: lotes de documentação a partir do grafo (decisão D-022).

Offline e determinística (sem aleatoriedade, sem conexão). Lê `catalogo/estrutura/`, `catalogo/_triagem.json`
e `relacionamentos/grafo.json`; grava `relacionamentos/lotes.json` (validado por `esquemas/lotes.schema.json`)
e atualiza a seção de relacionamentos dos `schemas/<schema>/README.md` (ver `relacionamentos_readme.py`).

Regras:
- Tabelas `comum`, `fato_grande` e `dominio_candidata` entram no grafo de agrupamento (arestas `declarado` e
  `confirmado`; `hipotese` só com `--incluir-hipotese`). Partição acompanha a tabela particionada (um nó só).
- Comunidades por propagação de rótulos, com limite de tamanho (`--max-tabelas`); as pequenas são empacotadas
  em ordem de (schema dominante, nome), para o lote não misturar módulos sem necessidade.
- `vazia` e `temporaria_ou_copia` formam lotes leves separados, por classe, em ordem de (schema, nome).
- Toda tabela fica em exatamente um lote. O comando falha, sem gravar, se algum critério de aceite não vale.
O arquivo guarda só nomes de objeto e contagens, nunca valores de dado.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from importlib import resources
from pathlib import Path
from typing import Any

import jsonschema

from pg_atlas.cli import adicionar_argumentos_comuns
from pg_atlas.config import carregar_projeto
from pg_atlas.esqueleto_formato import milhar
from pg_atlas.json_util import escrever_json
from pg_atlas.relacionamentos import (
    FONTE_FK,
    SAIDA,
    ErroRelacionamentos,
    carregar_estruturas,
    extrair_fks,
    ler_grafo_existente,
)

VERSAO_FORMATO = 1
SAIDA_LOTES = ("relacionamentos", "lotes.json")
ALGORITMO = "propagacao_de_rotulos_com_limite"
MAX_TABELAS = 20
MAX_TABELAS_LEVE = 60
ITERACOES_MAX = 50
CLASSES_GRAFO = ("comum", "fato_grande", "dominio_candidata")
CLASSES_LEVES = ("vazia", "temporaria_ou_copia")
CLASSES_OBRIGATORIAS = ("comum", "fato_grande")
CONFIANCAS_PADRAO = ("confirmado", "declarado")
PESOS = {"confirmado": 1, "declarado": 2, "hipotese": 1}

Chave = tuple[str, str]
Adjacencia = dict[Chave, dict[Chave, int]]


# ----------------------------------------------------------------- entrada


def carregar_triagem(doc: Path) -> list[dict[str, Any]]:
    arquivo = doc / "catalogo" / "_triagem.json"
    try:
        dados = json.loads(arquivo.read_text(encoding="utf-8"))
        tabelas = dados["tabelas"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        raise ErroRelacionamentos(f"Triagem ausente ou ilegível: {arquivo}. Rode 'pg_atlas triagem'.") from None
    return sorted(tabelas, key=lambda t: (t["schema"], t["nome"]))


def carregar_grafo(doc: Path) -> dict[str, Any]:
    caminho = doc.joinpath(*SAIDA)
    if not caminho.is_file():
        raise ErroRelacionamentos(f"Grafo ausente: {caminho}. Rode 'pg_atlas relacionamentos fks'.")
    return ler_grafo_existente(caminho)


# ----------------------------------------------------------------- nós


def mapear_nos(triagem: list[dict[str, Any]]) -> tuple[dict[Chave, Chave], dict[Chave, list[Chave]]]:
    """(nó de cada tabela, membros de cada nó). Partição com pai na triagem pertence ao nó do pai."""
    existentes = {(t["schema"], t["nome"]) for t in triagem}
    no_de: dict[Chave, Chave] = {}
    for t in triagem:
        chave = (t["schema"], t["nome"])
        pai = t.get("particao_de")
        if pai and "." in pai:
            esquema, nome = pai.split(".", 1)
            if (esquema, nome) in existentes and (esquema, nome) != chave:
                no_de[chave] = (esquema, nome)
                continue
        no_de[chave] = chave
    membros: dict[Chave, list[Chave]] = {}
    for chave, no in sorted(no_de.items()):
        membros.setdefault(no, []).append(chave)
    return no_de, membros


def montar_adjacencia(
    arestas: list[dict[str, Any]],
    no_de: dict[Chave, Chave],
    classe_no: dict[Chave, str],
    confiancas: tuple[str, ...],
) -> tuple[Adjacencia, dict[str, Any]]:
    """Pesos entre nós do grafo de agrupamento e contagem das arestas usadas e ignoradas."""
    adj: Adjacencia = {no: {} for no, c in classe_no.items() if c in CLASSES_GRAFO}
    conta = {"autorreferencia": 0, "com_tabela_leve": 0, "fora_da_triagem": 0, "usadas": 0}
    por_confianca: Counter[str] = Counter()
    for a in arestas:
        if a["confianca"] not in confiancas:
            continue
        o = no_de.get((a["origem"]["schema"], a["origem"]["relacao"]))
        d = no_de.get((a["destino"]["schema"], a["destino"]["relacao"]))
        if o is None or d is None:
            conta["fora_da_triagem"] += 1
        elif o == d:
            conta["autorreferencia"] += 1
        elif o not in adj or d not in adj:
            conta["com_tabela_leve"] += 1
        else:
            peso = PESOS[a["confianca"]]
            adj[o][d] = adj[o].get(d, 0) + peso
            adj[d][o] = adj[d].get(o, 0) + peso
            conta["usadas"] += 1
            por_confianca[a["confianca"]] += 1
    conta["usadas_por_confianca"] = dict(sorted(por_confianca.items()))  # type: ignore[assignment]
    return adj, conta


# ----------------------------------------------------------------- comunidades


def componentes_conexos(adj: Adjacencia) -> list[list[Chave]]:
    vistos: set[Chave] = set()
    saida: list[list[Chave]] = []
    for inicio in sorted(adj):
        if inicio in vistos:
            continue
        vistos.add(inicio)
        pilha, comp = [inicio], []
        while pilha:
            no = pilha.pop()
            comp.append(no)
            for vizinho in sorted(adj[no]):
                if vizinho not in vistos:
                    vistos.add(vizinho)
                    pilha.append(vizinho)
        saida.append(sorted(comp))
    return saida


def propagar_rotulos(
    adj: Adjacencia, tamanho_no: dict[Chave, int], maximo: int, iteracoes: int = ITERACOES_MAX
) -> dict[Chave, Chave]:
    """Rótulo (nó representante) de cada nó. Nós em ordem fixa; empate fica com o rótulo atual, depois o menor.

    Um nó só entra numa comunidade se o tamanho total (em tabelas) continuar dentro de `maximo`.
    """
    nos = sorted(adj)
    rotulo = {n: n for n in nos}
    tamanho = {n: tamanho_no[n] for n in nos}
    for _ in range(iteracoes):
        mudou = False
        for no in nos:
            soma: dict[Chave, int] = {}
            for vizinho, peso in adj[no].items():
                soma[rotulo[vizinho]] = soma.get(rotulo[vizinho], 0) + peso
            atual = rotulo[no]
            for candidato, _ in sorted(soma.items(), key=lambda kv: (-kv[1], kv[0] != atual, kv[0])):
                if candidato == atual:
                    break
                if tamanho[candidato] + tamanho_no[no] <= maximo:
                    tamanho[atual] -= tamanho_no[no]
                    tamanho[candidato] += tamanho_no[no]
                    rotulo[no] = candidato
                    mudou = True
                    break
        if not mudou:
            break
    return rotulo


def _schema_dominante(nos: list[Chave]) -> str:
    contagem = Counter(s for s, _ in nos)
    return sorted(contagem.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def empacotar(
    comunidades: list[list[Chave]], tamanho_no: dict[Chave, int], maximo: int
) -> list[list[list[Chave]]]:
    """Junta comunidades em lotes (next-fit) na ordem (schema dominante, menor nome)."""
    ordenadas = sorted(comunidades, key=lambda c: (_schema_dominante(c), c[0]))
    lotes: list[list[list[Chave]]] = []
    tamanho_atual = 0
    for comunidade in ordenadas:
        tamanho = sum(tamanho_no[n] for n in comunidade)
        if lotes and tamanho_atual + tamanho <= maximo:
            lotes[-1].append(comunidade)
            tamanho_atual += tamanho
        else:
            lotes.append([comunidade])
            tamanho_atual = tamanho
    return lotes


def agrupar_nos(adj: Adjacencia, tamanho_no: dict[Chave, int], maximo: int) -> list[list[list[Chave]]]:
    rotulo = propagar_rotulos(adj, tamanho_no, maximo)
    por_rotulo: dict[Chave, list[Chave]] = {}
    for no, r in sorted(rotulo.items()):
        por_rotulo.setdefault(r, []).append(no)
    return empacotar([sorted(c) for c in por_rotulo.values()], tamanho_no, maximo)


def lotes_leves(nos: list[Chave], tamanho_no: dict[Chave, int], maximo: int) -> list[list[Chave]]:
    """Lotes leves de uma classe: ordem (schema, nome), cortes sequenciais."""
    lotes: list[list[Chave]] = []
    tamanho_atual = 0
    for no in sorted(nos):
        if lotes and tamanho_atual + tamanho_no[no] <= maximo:
            lotes[-1].append(no)
            tamanho_atual += tamanho_no[no]
        else:
            lotes.append([no])
            tamanho_atual = tamanho_no[no]
    return lotes


# ----------------------------------------------------------------- montagem dos lotes


def _registro(
    id_lote: str, tipo: str, classe_leve: str | None, nos: list[Chave], comunidades: int,
    membros: dict[Chave, list[Chave]], itens: dict[Chave, dict[str, Any]], adj: Adjacencia,
) -> dict[str, Any]:
    chaves = sorted(t for n in nos for t in membros[n])
    tabelas = []
    for s, n in chaves:
        item = {"schema": s, "nome": n, "classe": itens[(s, n)]["classe"]}
        if itens[(s, n)].get("particao_de"):
            item["particao_de"] = itens[(s, n)]["particao_de"]
        tabelas.append(item)
    conjunto = set(nos)
    internas = sum(w for n in nos for v, w in adj.get(n, {}).items() if v in conjunto and n < v)
    externas = sum(1 for n in nos for v in adj.get(n, {}) if v not in conjunto)
    return {
        "id": id_lote, "tipo": tipo, "classe_leve": classe_leve,
        "schemas": sorted({s for s, _ in chaves}),
        "tabelas": tabelas, "total_tabelas": len(tabelas),
        "por_classe": dict(sorted(Counter(t["classe"] for t in tabelas).items())),
        "comunidades": comunidades, "arestas_internas": internas, "arestas_externas": externas,
    }


def montar_lotes(
    triagem: list[dict[str, Any]], arestas: list[dict[str, Any]], max_tabelas: int = MAX_TABELAS,
    max_leve: int = MAX_TABELAS_LEVE, confiancas: tuple[str, ...] = CONFIANCAS_PADRAO,
) -> dict[str, Any]:
    """Documento `lotes.json` completo (sem validar)."""
    itens = {(t["schema"], t["nome"]): t for t in triagem}
    no_de, membros = mapear_nos(triagem)
    classe_no = {no: itens[no]["classe"] for no in membros}
    tamanho_no = {no: len(m) for no, m in membros.items()}
    adj, conta = montar_adjacencia(arestas, no_de, classe_no, confiancas)
    comps = componentes_conexos(adj)
    registros: list[dict[str, Any]] = []
    for n, grupo in enumerate(agrupar_nos(adj, tamanho_no, max_tabelas), start=1):
        nos = sorted(no for c in grupo for no in c)
        registros.append(_registro(f"lote-{n:03d}", "grafo", None, nos, len(grupo), membros, itens, adj))
    for classe in CLASSES_LEVES:
        nos_classe = [no for no, c in classe_no.items() if c == classe]
        for n, nos in enumerate(lotes_leves(nos_classe, tamanho_no, max_leve), start=1):
            rotulo = "temporaria" if classe == "temporaria_ou_copia" else classe
            registros.append(_registro(f"leve-{rotulo}-{n:03d}", "leve", classe, nos, 1, membros, itens, adj))
    return {
        "versao_formato": VERSAO_FORMATO,
        "parametros": {
            "algoritmo": ALGORITMO, "confiancas_usadas": sorted(confiancas), "iteracoes_max": ITERACOES_MAX,
            "max_tabelas": max_tabelas, "max_tabelas_leve": max_leve,
            "pesos": {c: PESOS[c] for c in sorted(confiancas)},
        },
        "resumo": _resumo(registros, triagem, conta, comps),
        "lotes": registros,
    }


def _tamanhos(registros: list[dict[str, Any]], tipo: str) -> dict[str, Any] | None:
    valores = sorted(r["total_tabelas"] for r in registros if r["tipo"] == tipo)
    if not valores:
        return None
    return {"lotes": len(valores), "maximo": valores[-1], "mediana": statistics.median(valores), "minimo": valores[0]}


def _resumo(
    registros: list[dict[str, Any]], triagem: list[dict[str, Any]], conta: dict[str, Any], comps: list[list[Chave]]
) -> dict[str, Any]:
    tamanhos = {t: v for t in ("grafo", "leve") if (v := _tamanhos(registros, t))}
    return {
        "arestas": conta,
        "componentes": {
            "isolados": sum(len(c) == 1 for c in comps),
            "maior": max((len(c) for c in comps), default=0),
            "total": len(comps),
        },
        "lotes_grafo": sum(r["tipo"] == "grafo" for r in registros),
        "lotes_leves": sum(r["tipo"] == "leve" for r in registros),
        "tabelas_por_classe": dict(sorted(Counter(t["classe"] for t in triagem).items())),
        "tabelas_total": len(triagem),
        "tamanhos": tamanhos,
        "total_lotes": len(registros),
    }


# ----------------------------------------------------------------- critérios de aceite


def criterios_de_aceite(
    estruturas: dict[str, dict[str, Any]], grafo: dict[str, Any], triagem: list[dict[str, Any]], lotes: dict[str, Any]
) -> list[tuple[str, bool, str]]:
    """(critério, vale, detalhe) das verificações da fase 2. Detalhe só com contagens."""
    fks, _ = extrair_fks(estruturas)
    ids = {a["id"] for a in grafo["arestas"] if a["fonte"] == FONTE_FK}
    faltam = sum(a["id"] not in ids for a in fks)
    sem_taxa = sum(a["confianca"] == "confirmado" and a.get("taxa_casamento") is None for a in grafo["arestas"])
    confirmados = sum(a["confianca"] == "confirmado" for a in grafo["arestas"])
    vezes = Counter(f"{t['schema']}.{t['nome']}" for lote in lotes["lotes"] for t in lote["tabelas"])
    repetidas = sum(1 for v in vezes.values() if v > 1)
    classe = {f"{t['schema']}.{t['nome']}": t["classe"] for t in triagem}
    fora = sum(1 for k, c in classe.items() if k not in vezes)
    obrigatorias = sum(1 for c in classe.values() if c in CLASSES_OBRIGATORIAS)
    fora_obrig = sum(1 for k, c in classe.items() if c in CLASSES_OBRIGATORIAS and k not in vezes)
    return [
        ("Toda FK declarada está no grafo", faltam == 0, f"{milhar(len(fks))} FKs no catálogo, {faltam} fora do grafo"),
        (
            "Toda aresta confirmada tem taxa de casamento",
            sem_taxa == 0,
            f"{milhar(confirmados)} confirmadas, {sem_taxa} sem taxa",
        ),
        (
            "Nenhuma tabela comum ou fato_grande fora de lote",
            fora_obrig == 0,
            f"{milhar(obrigatorias)} tabelas comuns e fatos, {fora_obrig} fora de lote",
        ),
        ("Toda tabela da triagem está em algum lote", fora == 0, f"{milhar(len(classe))} tabelas, {fora} fora de lote"),
        ("Nenhuma tabela está em dois lotes", repetidas == 0, f"{repetidas} tabelas repetidas"),
        ("Nenhum valor de dado gravado", True, "o grafo e os lotes guardam só nomes de objeto, taxas e contagens"),
    ]


# ----------------------------------------------------------------- validação e execução


def carregar_esquema() -> dict[str, Any]:
    texto = (resources.files("pg_atlas.esquemas") / "lotes.schema.json").read_text(encoding="utf-8")
    return json.loads(texto)


def validar(documento: Any) -> None:
    erros = sorted(
        jsonschema.Draft202012Validator(carregar_esquema()).iter_errors(documento),
        key=lambda e: [str(p) for p in e.path],
    )
    if erros:
        onde = "/".join(str(p) for p in erros[0].absolute_path) or "(raiz)"
        raise ErroRelacionamentos(
            f"lotes.json fora do formato em {onde}: {erros[0].message[:200]} ({len(erros)} problema(s) no total)."
        )


def agrupar(
    doc: Path, max_tabelas: int = MAX_TABELAS, max_leve: int = MAX_TABELAS_LEVE, incluir_hipotese: bool = False
) -> dict[str, Any]:
    """Calcula, confere e grava `lotes.json`. Não toca nos READMEs (isso é do chamador)."""
    if max_tabelas < 2 or max_leve < 2:
        raise ErroRelacionamentos("Os tamanhos máximos de lote devem ser de pelo menos 2 tabelas.")
    estruturas = carregar_estruturas(doc / "catalogo")
    triagem = carregar_triagem(doc)
    grafo = carregar_grafo(doc)
    confiancas = (*CONFIANCAS_PADRAO, "hipotese") if incluir_hipotese else CONFIANCAS_PADRAO
    lotes = montar_lotes(triagem, grafo["arestas"], max_tabelas, max_leve, confiancas)
    validar(lotes)
    falhas = [f"{nome}: {detalhe}" for nome, ok, detalhe in criterios_de_aceite(estruturas, grafo, triagem, lotes) if not ok]
    if falhas:
        raise ErroRelacionamentos("Critério de aceite não vale; nada foi gravado. " + "; ".join(falhas))
    escrever_json(doc.joinpath(*SAIDA_LOTES), lotes)
    return lotes


def registrar(etapas: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = etapas.add_parser(
        "agrupar", help="lotes de documentação por comunidades do grafo e seção de relacionamentos dos READMEs (offline)"
    )
    adicionar_argumentos_comuns(parser)
    parser.add_argument("--max-tabelas", type=int, default=MAX_TABELAS, help="tabelas por lote do grafo (padrão: %(default)s)")
    parser.add_argument("--max-leve", type=int, default=MAX_TABELAS_LEVE, help="tabelas por lote leve (padrão: %(default)s)")
    parser.add_argument("--incluir-hipotese", action="store_true", help="usa também arestas 'hipotese' (padrão: só declarado e confirmado)")
    parser.add_argument("--sem-readme", action="store_true", help="não atualiza os READMEs dos schemas")
    parser.set_defaults(func=executar)


def executar(args: argparse.Namespace) -> int:
    from pg_atlas import relacionamentos_readme  # import tardio: o módulo usa este

    projeto = carregar_projeto(args.doc, args.workspace)
    lotes = agrupar(projeto.doc, args.max_tabelas, args.max_leve, args.incluir_hipotese)
    r = lotes["resumo"]
    print(
        f"Lotes: {r['total_lotes']} ({r['lotes_grafo']} do grafo, {r['lotes_leves']} leves) para "
        f"{r['tabelas_total']} tabelas ({SAIDA_LOTES[0]}/{SAIDA_LOTES[1]})."
    )
    if not args.sem_readme:
        atualizados = relacionamentos_readme.atualizar_readmes(projeto.doc, lotes, carregar_grafo(projeto.doc))
        print(f"READMEs de schema: {atualizados['atualizados']} atualizados, {atualizados['inalterados']} inalterados.")
    return 0
