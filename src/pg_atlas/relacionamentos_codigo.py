"""Etapa `relacionamentos codigo`: joins achados no código (decisão D-018).

Offline: lê `<doc>/catalogo/estrutura/*.json` (já mascarado), o `relacionamentos/grafo.json`
(precisa ter as arestas `fk_declarada`) e, de `config/fatos.yml`, só a chave `tenant.coluna`.
Reescreve só as arestas com `fonte == "inferencia_codigo"` e preserva as demais e os blocos de resumo
das outras etapas. Grava também `_controle/codigo-nao-analisado.json` (só nomes de objeto e contagens).

A extração dos comandos está em `codigo_extracao.py` e a análise dos joins em `codigo_joins.py`.
O grafo guarda só metadado: nunca trecho de código nem valores de dado.
"""

from __future__ import annotations

import argparse
import logging
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterator, NamedTuple

import sqlglot

from pg_atlas.cli import adicionar_argumentos_comuns
from pg_atlas.codigo_extracao import (
    EXTRATOR_HEURISTICO,
    EXTRATOR_PGLAST,
    Extracao,
    extrair_definicao,
    extrair_rotina,
    pglast_disponivel,
)
from pg_atlas.codigo_joins import (
    DESCARTES,
    NOTA_UM_PARA_UM,
    AnalisadorDeJoins,
    IndiceCatalogo,
    Ligacao,
    contadores_zerados,
)
from pg_atlas.config import carregar_projeto
from pg_atlas.json_util import escrever_json
from pg_atlas.relacionamentos import (
    FONTE_FK,
    SAIDA,
    ErroRelacionamentos,
    carregar_estruturas,
    avisar_verificacao_obsoleta,
    ler_grafo_existente,
    montar_grafo,
    validar,
)
from pg_atlas.relacionamentos_nome import FONTE_NOME, coluna_de_tenant

FONTE_CODIGO = "inferencia_codigo"
SAIDA_NAO_ANALISADO = ("_controle", "codigo-nao-analisado.json")
TAMANHO_MAXIMO_COMANDO = 100_000
TETO_OBJETOS = 20
TIPOS_VIEW = ("view", "materializada")
TIPOS_ROTINA = ("function", "procedure")
LINGUAGENS_ANALISADAS = ("sql", "plpgsql")

ObjetoId = tuple[str, str, str]  # tipo, schema, nome
ChaveAresta = tuple[tuple[str, str], tuple[str, ...], tuple[str, str], tuple[str, ...]]


class Objeto(NamedTuple):
    tipo: str
    schema: str
    nome: str
    extracao: Extracao | None  # None: o objeto fica fora (sem código analisável)

    @property
    def id(self) -> ObjetoId:
        return (self.tipo, self.schema, self.nome)


# ----------------------------------------------------------------- objetos


def coletar_objetos(estruturas: dict[str, dict[str, Any]], usar_pglast: bool) -> Iterator[Objeto]:
    """Views, materializadas e rotinas, em ordem de schema e nome, com os comandos já extraídos."""
    for schema in sorted(estruturas):
        doc = estruturas[schema]
        for rel in sorted(doc.get("relacoes", []), key=lambda r: r["nome"]):
            if rel.get("tipo") in TIPOS_VIEW:
                definicao = rel.get("definicao")
                extracao = extrair_definicao(definicao) if definicao else None
                yield Objeto(rel["tipo"], schema, rel["nome"], extracao)
        for fn in sorted(doc.get("funcoes", []), key=lambda f: (f["nome"], f.get("argumentos_identidade") or "")):
            codigo, linguagem = fn.get("codigo"), fn.get("linguagem")
            extracao = None
            if fn.get("tipo") in TIPOS_ROTINA and linguagem in LINGUAGENS_ANALISADAS and codigo:
                extracao = extrair_rotina(codigo, linguagem, usar_pglast)
            yield Objeto(fn.get("tipo") or "function", schema, fn["nome"], extracao)


# ----------------------------------------------------------------- análise


def motivo_de_parse(comando: str) -> str:
    return "mascarado" if "[mascarado:" in comando else "parse"


def analisar_comando(
    analisador: AnalisadorDeJoins, comando: str, schema: str
) -> tuple[list[Ligacao], dict[str, int]] | str:
    """Ligações e contagens do comando, ou o motivo da falha (`parse`, `mascarado`, `analise`)."""
    if len(comando) > TAMANHO_MAXIMO_COMANDO:
        return "comando_grande"
    try:
        arvores = sqlglot.parse(comando, read="postgres")
    except Exception:  # inclui RecursionError: o motivo é contado e o objeto segue
        return motivo_de_parse(comando)
    try:
        return analisador.analisar_arvores(arvores, schema)
    except Exception:
        return "analise"


class Acumulado:
    """Uma aresta candidata agregada por (origem, colunas, destino, colunas)."""

    def __init__(self) -> None:
        self.objetos: set[ObjetoId] = set()
        self.tipos_iguais = True
        self.chave_sem_tenant = False
        self.um_para_um = False

    def somar(self, lig: Ligacao, objeto: ObjetoId) -> None:
        self.objetos.add(objeto)
        self.tipos_iguais = self.tipos_iguais and lig.tipos_iguais
        self.chave_sem_tenant = self.chave_sem_tenant or lig.chave_sem_tenant
        self.um_para_um = self.um_para_um or lig.um_para_um


def _id_aresta(chave: ChaveAresta) -> str:
    (so, ro), co, (sd, rd), cd = chave
    return f"codigo:{so}.{ro}.{','.join(co)}->{sd}.{rd}.{','.join(cd)}"


def montar_aresta(chave: ChaveAresta, acc: Acumulado) -> dict[str, Any]:
    (so, ro), co, (sd, rd), cd = chave
    objetos = sorted(acc.objetos)
    return {
        "id": _id_aresta(chave),
        "origem": {"schema": so, "relacao": ro, "colunas": list(co)},
        "destino": {"schema": sd, "relacao": rd, "colunas": list(cd)},
        "confianca": "hipotese",
        "fonte": FONTE_CODIGO,
        "taxa_casamento": None,
        "nota": NOTA_UM_PARA_UM if acc.um_para_um else None,
        "evidencia": [
            {
                "tipo": FONTE_CODIGO,
                "objetos": [{"schema": s, "nome": n, "tipo": t} for t, s, n in objetos[:TETO_OBJETOS]],
                "total_objetos": len(objetos),
                "tipos_iguais": acc.tipos_iguais,
                "chave_sem_tenant": acc.chave_sem_tenant,
            }
        ],
    }


def _pares(origem: dict[str, Any], destino: dict[str, Any]) -> tuple[tuple[str, str], tuple[str, str], frozenset[tuple[str, str]]]:
    return (
        (origem["schema"], origem["relacao"]),
        (destino["schema"], destino["relacao"]),
        frozenset(zip(origem["colunas"], destino["colunas"])),
    )


def contar_desempates(arestas_codigo: list[dict[str, Any]], arestas_nome: list[dict[str, Any]]) -> int:
    """Colunas ambíguas da inferência por nome em que o código confirma exatamente um dos empatados."""
    do_codigo: dict[tuple[str, str, str], set[tuple[str, str, str]]] = {}
    for a in arestas_codigo:
        o, d = a["origem"], a["destino"]
        if len(o["colunas"]) == 1:
            do_codigo.setdefault((o["schema"], o["relacao"], o["colunas"][0]), set()).add(
                (d["schema"], d["relacao"], d["colunas"][0])
            )
    empatados: dict[tuple[str, str, str], set[tuple[str, str, str]]] = {}
    for a in arestas_nome:
        if a["evidencia"][0].get("candidatos", 1) > 1:
            o, d = a["origem"], a["destino"]
            empatados.setdefault((o["schema"], o["relacao"], o["colunas"][0]), set()).add(
                (d["schema"], d["relacao"], d["colunas"][0])
            )
    return sum(1 for col, alvos in empatados.items() if len(alvos & do_codigo.get(col, set())) == 1)


def inferir(
    estruturas: dict[str, dict[str, Any]],
    arestas_existentes: list[dict[str, Any]],
    tenant: str | None,
    forcar_heuristico: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """Arestas `inferencia_codigo` (por `id`), o bloco `resumo.inferencia_codigo` e os itens não analisados."""
    logging.getLogger("sqlglot").setLevel(logging.CRITICAL)
    usar_pglast = not forcar_heuristico and pglast_disponivel()
    analisador = AnalisadorDeJoins(IndiceCatalogo(estruturas), tenant)
    cont = contadores_zerados()
    analisados: dict[str, set[ObjetoId]] = {}
    fora: dict[str, set[ObjetoId]] = {}
    falhos: Counter[str] = Counter()
    fallback: Counter[str] = Counter()
    execute = comandos_ok = 0
    por_objeto: dict[ObjetoId, tuple[int, Counter[str]]] = {}
    acumulado: dict[ChaveAresta, Acumulado] = {}
    for obj in coletar_objetos(estruturas, usar_pglast):
        if obj.extracao is None:
            fora.setdefault(obj.tipo, set()).add(obj.id)
            continue
        analisados.setdefault(obj.tipo, set()).add(obj.id)
        execute += obj.extracao.execute_dinamico
        if obj.extracao.fallback:
            fallback[obj.extracao.fallback] += 1
        total, motivos = por_objeto.get(obj.id, (0, Counter()))
        for comando in obj.extracao.comandos:
            total += 1
            resultado = analisar_comando(analisador, comando, obj.schema)
            if isinstance(resultado, str):
                motivos[resultado] += 1
                falhos[resultado] += 1
                continue
            comandos_ok += 1
            ligacoes, contagens = resultado
            for k, v in contagens.items():
                cont[k] += v
            for lig in ligacoes:
                chave = (lig.origem, lig.colunas_origem, lig.destino, lig.colunas_destino)
                acumulado.setdefault(chave, Acumulado()).somar(lig, obj.id)
        por_objeto[obj.id] = (total, motivos)
    arestas, repetem_fk, repetem_nome = _montar_arestas(acumulado, arestas_existentes)
    nome = [a for a in arestas_existentes if a["fonte"] == FONTE_NOME]
    fora_final = {t: ids - analisados.get(t, set()) for t, ids in fora.items()}
    bloco = {
        "extrator": EXTRATOR_PGLAST if usar_pglast else EXTRATOR_HEURISTICO,
        "objetos_analisados": {t: len(ids) for t, ids in sorted(analisados.items())},
        "objetos_fora": {t: len(ids) for t, ids in sorted(fora_final.items()) if ids},
        "comandos_analisados": comandos_ok,
        "comandos_falhos": dict(sorted(falhos.items())),
        "fallback_extracao": dict(sorted(fallback.items())),
        "execute_dinamico": execute,
        "igualdades": cont["igualdades"],
        **{d: cont[d] for d in DESCARTES},
        "arestas": len(arestas),
        "um_para_um": sum(a["nota"] is not None for a in arestas),
        "repetem_fk": repetem_fk,
        "repetem_nome": repetem_nome,
        "desempata_nome": contar_desempates(arestas, nome),
    }
    return arestas, bloco, _itens_nao_analisados(por_objeto)


def _montar_arestas(
    acumulado: dict[ChaveAresta, Acumulado], existentes: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int, int]:
    fks = {_pares(a["origem"], a["destino"]) for a in existentes if a["fonte"] == FONTE_FK}
    nomes = {_pares(a["origem"], a["destino"]) for a in existentes if a["fonte"] == FONTE_NOME}
    arestas: list[dict[str, Any]] = []
    repetem_fk = repetem_nome = 0
    for chave in sorted(acumulado):
        (so, ro), co, (sd, rd), cd = chave
        par = ((so, ro), (sd, rd), frozenset(zip(co, cd)))
        if par in fks:
            repetem_fk += 1
            continue
        repetem_nome += par in nomes
        arestas.append(montar_aresta(chave, acumulado[chave]))
    return sorted(arestas, key=lambda a: a["id"]), repetem_fk, repetem_nome


def _itens_nao_analisados(por_objeto: dict[ObjetoId, tuple[int, Counter[str]]]) -> list[dict[str, Any]]:
    itens = []
    for (tipo, schema, nome), (total, motivos) in sorted(por_objeto.items(), key=lambda i: (i[0][1], i[0][2], i[0][0])):
        if not motivos:
            continue
        principal = min(motivos, key=lambda m: (-motivos[m], m))
        itens.append(
            {
                "schema": schema,
                "nome": nome,
                "tipo": tipo,
                "motivo": principal,
                "comandos_falhos": sum(motivos.values()),
                "comandos_total": total,
            }
        )
    return itens


# ----------------------------------------------------------------- execução


def executar_codigo(doc: Path, forcar_heuristico: bool = False) -> dict[str, Any]:
    estruturas = carregar_estruturas(doc / "catalogo")
    caminho = doc.joinpath(*SAIDA)
    if not caminho.is_file():
        raise ErroRelacionamentos(f"{caminho} não existe. Rode 'relacionamentos fks' antes.")
    existente = ler_grafo_existente(caminho)
    if not any(a["fonte"] == FONTE_FK for a in existente["arestas"]):
        raise ErroRelacionamentos("O grafo não tem arestas fk_declarada. Rode 'relacionamentos fks' antes.")
    avisar_verificacao_obsoleta(existente["resumo"])
    novas, bloco, itens = inferir(estruturas, existente["arestas"], coluna_de_tenant(doc), forcar_heuristico)
    outras = [a for a in existente["arestas"] if a["fonte"] != FONTE_CODIGO]
    grafo = montar_grafo(outras, novas, existente["resumo"].get("inferencia_nome"), bloco)
    validar(grafo)
    escrever_json(caminho, grafo)
    escrever_json(
        doc.joinpath(*SAIDA_NAO_ANALISADO),
        {"formato": "codigo_nao_analisado", "versao_formato": 1, "itens": itens},
    )
    return grafo


def registrar(etapas: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    codigo = etapas.add_parser("codigo", help="joins achados em views e rotinas, confiança 'hipotese' (offline)")
    adicionar_argumentos_comuns(codigo)
    codigo.add_argument(
        "--extrator",
        choices=("auto", EXTRATOR_HEURISTICO),
        default="auto",
        help="'auto' usa o pglast se estiver instalado; 'heuristico' força a extração sem pglast",
    )
    codigo.set_defaults(func=executar)


def executar(args: argparse.Namespace) -> int:
    inicio = time.monotonic()
    projeto = carregar_projeto(args.doc, args.workspace)
    grafo = executar_codigo(projeto.doc, forcar_heuristico=args.extrator == EXTRATOR_HEURISTICO)
    b = grafo["resumo"]["inferencia_codigo"]
    print(f"Grafo: {grafo['resumo']['total']} arestas ({SAIDA[0]}/{SAIDA[1]}); {FONTE_CODIGO}={b['arestas']}.")
    print(f"Extrator: {b['extrator']}; comandos analisados={b['comandos_analisados']}; EXECUTE dinâmico={b['execute_dinamico']}.")
    print("Objetos analisados: " + ", ".join(f"{t}={n}" for t, n in b["objetos_analisados"].items()) + ".")
    print("Objetos fora: " + ", ".join(f"{t}={n}" for t, n in b["objetos_fora"].items()) + ".")
    print("Comandos falhos: " + ", ".join(f"{m}={n}" for m, n in b["comandos_falhos"].items()) + ".")
    print(f"Igualdades={b['igualdades']}; " + ", ".join(f"{d}={b[d]}" for d in DESCARTES) + ".")
    print(
        f"Arestas: 1:1={b['um_para_um']}, repetem FK={b['repetem_fk']}, "
        f"repetem nome={b['repetem_nome']}, desempata nome={b['desempata_nome']}."
    )
    print(f"Tempo de execução: {time.monotonic() - inicio:.1f} s.")
    return 0
