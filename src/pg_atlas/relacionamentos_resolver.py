"""Etapa `relacionamentos resolver`: aplica ao grafo o que o analista decidiu (decisão D-021, item 4).

Offline. Lê `_controle/lotes/relacionamentos-<id>.resolvido.json`, valida cada um por
`esquemas/relacionamentos-resolvido.schema.json` e confere com o lote correspondente. Idempotente: primeiro
desfaz toda análise já gravada (a evidência `analise` guarda `nota_anterior` e `confianca_anterior`) e só então
reaplica tudo. Nunca promove a `confirmado`. Colunas P3 (lista de `lotes`) só recebem a marca `sem_evidencia`.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from importlib import resources
from pathlib import Path
from typing import Any

import jsonschema

from pg_atlas.cli import adicionar_argumentos_comuns
from pg_atlas.config import carregar_projeto
from pg_atlas.json_util import escrever_json
from pg_atlas.relacionamentos import (
    FONTE_FK,
    SAIDA,
    TIPO_ANALISE,
    ErroRelacionamentos,
    ler_grafo_existente,
    montar_grafo,
    validar,
)
from pg_atlas.relacionamentos_lotes import (
    ARQ_INDICE,
    ChaveColuna,
    PASTA_LOTES,
    SAIDA_SEM_EVIDENCIA,
    amostragem_de,
    chave_origem,
    eh_ambigua,
    id_coluna,
)
from pg_atlas.verificar_aplicar import PREFIXO_AMBIGUO, _juntar, _segmentos

PREFIXO_PRETERIDO = "preterido pelo analista:"


class Contagens(dict):  # type: ignore[type-arg]
    """Contadores do `resumo.analise`, zerados."""

    def __init__(self) -> None:
        super().__init__(
            escolhidas=0, duvidas=0, sem_evidencia=0, arestas_preteridas=0, arestas_descartadas=0,
            grau_ajustado=0, descarte_convertido=0, itens_rejeitados=0,
        )


# ----------------------------------------------------------------- esquema


def carregar_esquema_resolvido() -> dict[str, Any]:
    texto = (resources.files("pg_atlas.esquemas") / "relacionamentos-resolvido.schema.json").read_text(encoding="utf-8")
    return json.loads(texto)


def _validador(sub: str | None = None) -> jsonschema.Draft202012Validator:
    esquema = carregar_esquema_resolvido()
    if sub is not None:
        esquema = {"$schema": esquema["$schema"], "$defs": esquema["$defs"], "$ref": f"#/$defs/{sub}"}
    return jsonschema.Draft202012Validator(esquema)


def _primeiro_erro(validador: jsonschema.Draft202012Validator, doc: Any) -> str | None:
    erros = sorted(validador.iter_errors(doc), key=lambda e: [str(p) for p in e.path])
    if not erros:
        return None
    onde = "/".join(str(p) for p in erros[0].absolute_path) or "(raiz)"
    return f"{onde}: {erros[0].message[:160]}"


def _envelope_valido(doc: Any) -> str | None:
    """Erro do envelope do arquivo (sem olhar o conteúdo das colunas), ou None."""
    if not isinstance(doc, dict):
        return "(raiz): deveria ser um objeto"
    return _primeiro_erro(_validador(), {**doc, "colunas": []}) or (
        None if isinstance(doc.get("colunas"), list) else "colunas: deveria ser uma lista"
    )


# ----------------------------------------------------------------- desfazer


def desfazer_analise(arestas: list[dict[str, Any]]) -> None:
    """Remove a evidência `analise` e restaura nota e confiança anteriores (no lugar)."""
    for a in arestas:
        for e in a["evidencia"]:
            if e.get("tipo") != TIPO_ANALISE:
                continue
            if "nota_anterior" in e:
                a["nota"] = e["nota_anterior"]
            if "confianca_anterior" in e:
                a["confianca"] = e["confianca_anterior"]
        a["evidencia"] = [e for e in a["evidencia"] if e.get("tipo") != TIPO_ANALISE]


# ----------------------------------------------------------------- leitura dos arquivos


def _ler_json(caminho: Path) -> Any:
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def ler_resolvidos(pasta: Path, cont: Contagens) -> dict[str, dict[str, Any]]:
    """Resolvidos de envelope válido, por id de lote (ordem de arquivo). Inválidos contam como rejeitados."""
    validos: dict[str, dict[str, Any]] = {}
    for arquivo in sorted(pasta.glob("relacionamentos-*.resolvido.json")) if pasta.is_dir() else []:
        doc = _ler_json(arquivo)
        erro = "JSON ilegível" if doc is None else _envelope_valido(doc)
        lote_arq = arquivo.name[len("relacionamentos-") : -len(".resolvido.json")]
        lote = _ler_json(pasta / f"relacionamentos-{lote_arq}.json")
        if erro is None and (not isinstance(lote, dict) or doc["lote"] != lote_arq):
            erro = "lote inexistente ou diferente do nome do arquivo"
        if erro is not None:
            cont["itens_rejeitados"] += 1
            print(f"Rejeitado {arquivo.name}: {erro}", file=sys.stderr)
            continue
        validos[lote_arq] = {"resolvido": doc, "lote": lote}
    return validos


# ----------------------------------------------------------------- aplicação


def _trocar_ambiguo(a: dict[str, Any], novo: str | None) -> None:
    """Tira os segmentos "ambíguo" da nota e acrescenta `novo` (se houver)."""
    segmentos = [s for s in _segmentos(a["nota"]) if not s.startswith(PREFIXO_AMBIGUO)]
    if novo:
        segmentos.insert(0, novo)
    a["nota"] = _juntar(segmentos)


def _evidencia(a: dict[str, Any], resultado: str, lote: str, justificativa: str, **extra: Any) -> None:
    e = {"tipo": TIPO_ANALISE, "resultado": resultado, "lote": lote, "justificativa": justificativa}
    e.update(extra)
    a["evidencia"].append(e)


def _validar_coluna(
    item: dict[str, Any], coluna_lote: dict[str, Any] | None, indice: dict[str, dict[str, Any]]
) -> str | None:
    """Motivo da rejeição (só nomes de objeto) ou None."""
    if coluna_lote is None:
        return "coluna fora do lote"
    ids_lote = {d["aresta"] for d in coluna_lote["destinos"]}
    citados = [d["aresta"] for d in item.get("destinos", [])]
    if any(c not in ids_lote for c in citados):
        return "destino fora do lote"
    if len(set(citados)) != len(citados):
        return "destino repetido"
    for c in citados:
        if c in indice and indice[c]["fonte"] == FONTE_FK:
            return "destino é FK declarada"
    escolhidos = [d for d in item.get("destinos", []) if d["acao"] == "escolhido"]
    if item["resultado"] == "escolhido":
        if len(escolhidos) != 1:
            return "'escolhido' exige exatamente um destino escolhido"
        if escolhidos[0]["aresta"] not in indice:
            return "aresta escolhida não existe mais no grafo"
    elif escolhidos:
        return "'duvida' não pode ter destino escolhido"
    return None


def _taxa_util_alta(a: dict[str, Any], minimo: float, amostra_min: int) -> bool:
    am = amostragem_de(a)
    taxa = a["taxa_casamento"]
    return taxa is not None and taxa >= minimo and am is not None and am.get("amostrados", 0) >= amostra_min


def _aplicar_escolhido(
    item: dict[str, Any], coluna_lote: dict[str, Any], lote: str, indice: dict[str, dict[str, Any]],
    removidas: set[str], cont: Contagens, lim_hipotese: float, amostra_min: int,
) -> None:
    acoes = {d["aresta"]: d for d in item["destinos"]}
    escolhido = next(d for d in item["destinos"] if d["acao"] == "escolhido")
    a = indice[escolhido["aresta"]]
    pedido = escolhido.get("grau")
    if pedido == "confirmado" and a["confianca"] != "confirmado":
        cont["grau_ajustado"] += 1
    _evidencia(a, "escolhido", lote, escolhido["justificativa"], nota_anterior=a["nota"])
    _trocar_ambiguo(a, None)
    cont["escolhidas"] += 1
    for d in coluna_lote["destinos"]:
        outro = d["aresta"]
        if outro == escolhido["aresta"]:
            continue
        acao = acoes.get(outro, {"acao": "preterido", "justificativa": item["justificativa"]})
        if outro not in indice:
            if acao["acao"] == "descartado":  # já descartada numa aplicação anterior
                cont["arestas_descartadas"] += 1
            continue
        b = indice[outro]
        if acao["acao"] == "descartado" and _taxa_util_alta(b, lim_hipotese, amostra_min):
            cont["descarte_convertido"] += 1
            acao = {**acao, "acao": "preterido"}
        if acao["acao"] == "descartado":
            removidas.add(outro)
            cont["arestas_descartadas"] += 1
            continue
        extra: dict[str, Any] = {"nota_anterior": b["nota"], "escolhido": escolhido["aresta"]}
        if b["confianca"] != "hipotese":
            extra["confianca_anterior"] = b["confianca"]
        _evidencia(b, "preterido", lote, acao["justificativa"], **extra)
        b["confianca"] = "hipotese"
        _trocar_ambiguo(b, f"{PREFIXO_PRETERIDO} escolhido {escolhido['aresta']}")
        cont["arestas_preteridas"] += 1


def _aplicar_duvida(
    item: dict[str, Any], coluna_lote: dict[str, Any], lote: str, indice: dict[str, dict[str, Any]], cont: Contagens
) -> None:
    extra = {"verificacao": item["verificacao"]} if item.get("verificacao") else {}
    for d in coluna_lote["destinos"]:
        if d["aresta"] in indice:
            _evidencia(indice[d["aresta"]], "duvida", lote, item["justificativa"], **extra)
    cont["duvidas"] += 1


def aplicar_resolvidos(
    arestas: list[dict[str, Any]], resolvidos: dict[str, dict[str, Any]], cont: Contagens,
    lim_hipotese: float, amostra_min: int,
) -> list[dict[str, Any]]:
    """Aplica os resolvidos às arestas (já desfeitas). Devolve as arestas que restam."""
    indice = {a["id"]: a for a in arestas}
    removidas: set[str] = set()
    decididas: dict[str, tuple[str, dict[str, Any], dict[str, Any]]] = {}  # coluna -> (lote, item, coluna do lote)
    for lote in sorted(resolvidos):
        validador_coluna = _validador("coluna")
        por_coluna = {c["id_coluna"]: c for c in resolvidos[lote]["lote"]["colunas"]}
        for item in resolvidos[lote]["resolvido"]["colunas"]:
            erro = _primeiro_erro(validador_coluna, item)
            if erro is None:
                erro = _validar_coluna(item, por_coluna.get(item["coluna"]), indice)
            if erro is not None:
                cont["itens_rejeitados"] += 1
                print(f"Rejeitado em {lote}: {erro}", file=sys.stderr)
                continue
            decididas[item["coluna"]] = (lote, item, por_coluna[item["coluna"]])
    for coluna in sorted(decididas):
        lote, item, coluna_lote = decididas[coluna]
        if item["resultado"] == "escolhido":
            _aplicar_escolhido(item, coluna_lote, lote, indice, removidas, cont, lim_hipotese, amostra_min)
        else:
            _aplicar_duvida(item, coluna_lote, lote, indice, cont)
    return [a for a in arestas if a["id"] not in removidas]


def marcar_sem_evidencia(arestas: list[dict[str, Any]], doc: Path, cont: Contagens) -> None:
    """Colunas P3 ainda ambíguas e sem decisão do analista recebem `sem_evidencia`; nada mais muda."""
    dados = _ler_json(doc.joinpath(*SAIDA_SEM_EVIDENCIA))
    if not isinstance(dados, dict):
        return
    p3 = {c["id_coluna"] for c in dados.get("colunas", []) if isinstance(c, dict) and "id_coluna" in c}
    grupos: dict[ChaveColuna, list[dict[str, Any]]] = {}
    for a in arestas:
        if a["fonte"] != FONTE_FK:
            grupos.setdefault(chave_origem(a), []).append(a)
    for chave in sorted(grupos):
        membros = grupos[chave]
        if id_coluna(chave) not in p3 or not any(eh_ambigua(a) for a in membros):
            continue
        if any(e.get("tipo") == TIPO_ANALISE for a in membros for e in a["evidencia"]):
            continue
        for a in membros:
            a["evidencia"].append({"tipo": TIPO_ANALISE, "resultado": "sem_evidencia"})
        cont["sem_evidencia"] += 1


# ----------------------------------------------------------------- execução


def _situacao_no_indice(pasta: Path, indice: dict[str, Any], resolvidos: set[str]) -> None:
    for entrada in indice["lotes"]:
        entrada["situacao"] = "resolvido" if entrada["lote"] in resolvidos else "pendente"


def executar_resolver(doc: Path, taxa_hipotese: float, amostra_min: int) -> dict[str, Any]:
    caminho = doc.joinpath(*SAIDA)
    if not caminho.is_file():
        raise ErroRelacionamentos(f"{caminho} não existe. Rode 'relacionamentos fks' antes.")
    pasta = doc.joinpath(*PASTA_LOTES)
    indice = _ler_json(pasta / ARQ_INDICE)
    if not isinstance(indice, dict) or "lotes" not in indice:
        raise ErroRelacionamentos(f"{pasta / ARQ_INDICE} não existe. Rode 'relacionamentos lotes' antes.")
    existente = ler_grafo_existente(caminho)
    resumo = existente["resumo"]
    arestas = copy.deepcopy(existente["arestas"])
    desfazer_analise(arestas)
    cont = Contagens()
    resolvidos = ler_resolvidos(pasta, cont)
    arestas = aplicar_resolvidos(arestas, resolvidos, cont, taxa_hipotese, amostra_min)
    marcar_sem_evidencia(arestas, doc, cont)
    _situacao_no_indice(pasta, indice, set(resolvidos))
    fila = indice.get("resumo", {}).get("colunas_fila", {})
    bloco = {
        "colunas_fila": {n: int(fila.get(n, 0)) for n in ("p1", "p2", "p3")},
        "lotes": {"gerados": len(indice["lotes"]), "resolvidos": len(resolvidos)},
        **cont,
    }
    grafo = montar_grafo(
        arestas, [], resumo.get("inferencia_nome"), resumo.get("inferencia_codigo"), resumo.get("verificacao"), bloco
    )
    validar(grafo)
    escrever_json(caminho, grafo)
    escrever_json(pasta / ARQ_INDICE, indice)
    return grafo


def registrar(etapas: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = etapas.add_parser("resolver", help="aplica ao grafo os lotes resolvidos pelo analista (offline)")
    adicionar_argumentos_comuns(p)
    p.set_defaults(func=executar)


def executar(args: argparse.Namespace) -> int:
    projeto = carregar_projeto(args.doc, args.workspace)
    grafo = executar_resolver(projeto.doc, projeto.taxa_hipotese, projeto.amostra_min_valores)
    b = grafo["resumo"]["analise"]
    print(f"Grafo: {grafo['resumo']['total']} arestas; lotes gerados={b['lotes']['gerados']}, resolvidos={b['lotes']['resolvidos']}.")
    print(
        f"Colunas: escolhidas={b['escolhidas']}, dúvidas={b['duvidas']}, sem evidência={b['sem_evidencia']}; "
        f"arestas preteridas={b['arestas_preteridas']}, descartadas={b['arestas_descartadas']}."
    )
    print(
        f"Ajustes: grau ajustado={b['grau_ajustado']}, descarte convertido={b['descarte_convertido']}, "
        f"itens rejeitados={b['itens_rejeitados']}."
    )
    return 0
