"""Etapa `relacionamentos lotes`: fila e lotes do analista de relacionamentos (decisão D-021).

Offline e determinística. Lê o catálogo mascarado, o `grafo.json` e `config/fatos.yml` (nunca
`tenant.valores`). Classifica, sem LLM, cada coluna de origem que ainda tem a nota "ambíguo":

- P1: algum destino tem taxa medida com amostra de pelo menos `amostra_min_valores`;
- P2: sem taxa útil, mas há aresta de código, comentário que cita um destino ou fato de `fatos.yml`;
- P3: sem evidência que diferencie (não vai ao analista; só a lista de nomes é gravada).

P1 e P2 viram lotes `_controle/lotes/relacionamentos-<id>.json` (até 25 colunas, cerca de 150 KB), mais o
índice `relacionamentos-indice.json`. O lote leva só metadado do catálogo mascarado, taxas e trechos de código
sanitizados (strings viram `'?'`, números com mais de 4 dígitos viram `0`). Nenhum valor vem do banco.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from pg_atlas.cli import adicionar_argumentos_comuns
from pg_atlas.config import carregar_projeto
from pg_atlas.json_util import escrever_json, serializar_json
from pg_atlas.relacionamentos import (
    FONTE_FK,
    SAIDA,
    ErroRelacionamentos,
    carregar_estruturas,
    ler_grafo_existente,
)
from pg_atlas.relacionamentos_codigo import FONTE_CODIGO
from pg_atlas.relacionamentos_nome import FONTE_NOME
from pg_atlas.verificar_amostragem import ler_estatisticas, linhas_estimadas, mapa_de_particoes
from pg_atlas.verificar_aplicar import PREFIXO_AMBIGUO, _segmentos

MAX_COLUNAS = 25
MAX_BYTES = 150_000
MAX_OBJETOS = 3
MAX_LINHAS = 15
MAX_CHARS_LINHA = 300
FOLGA_CABECALHO = 4_000  # cabeçalho e formato_saida do lote
PASTA_LOTES = ("_controle", "lotes")
ARQ_INDICE = "relacionamentos-indice.json"
SAIDA_SEM_EVIDENCIA = ("_controle", "relacionamentos-sem-evidencia.json")
NIVEIS = ("p1", "p2")
CHAVES_FATOS_IGNORADAS = ("banco", "credencial", "tenant")
FONTES_INFERIDAS = (FONTE_NOME, FONTE_CODIGO)
PADRAO_LOTE = re.compile(r"^relacionamentos-(p[12]-\d{3,})\.json$")

ChaveColuna = tuple[str, str, tuple[str, ...]]  # schema, relação, colunas de origem

FORMATO_SAIDA: dict[str, Any] = {
    "arquivo": "relacionamentos-<lote>.resolvido.json (na mesma pasta deste lote)",
    "esquema": "pg_atlas/esquemas/relacionamentos-resolvido.schema.json",
    "estrutura": {
        "formato": "relacionamentos_resolvido",
        "versao_formato": 1,
        "lote": "<id deste lote>",
        "colunas": [
            {
                "coluna": "<campo id_coluna da coluna do lote>",
                "resultado": "escolhido | duvida",
                "justificativa": "texto curto, até 300 caracteres, sem valor de dado",
                "verificacao": "(opcional) nome do arquivo em _controle/verificacoes/pendentes/",
                "destinos": [
                    {
                        "aresta": "<campo aresta de um destino desta coluna>",
                        "acao": "escolhido | preterido | descartado",
                        "grau": "(opcional, só em escolhido) confirmado | hipotese",
                        "justificativa": "texto curto, até 300 caracteres, sem valor de dado",
                    }
                ],
            }
        ],
    },
    "regras": [
        "resultado 'escolhido' exige exatamente um destino com acao 'escolhido'.",
        "Destino não citado numa coluna 'escolhido' é tratado como 'preterido': a aresta fica no grafo como hipotese.",
        "'descartado' só vale para destino sem taxa útil maior ou igual ao limiar de hipótese e com evidência "
        "positiva contra ele; caso contrário o resolver converte em 'preterido'.",
        "'grau' confirmado só é aceito se a aresta já estiver confirmada pela amostragem; o resolver nunca promove.",
        "resultado 'duvida' não muda arestas; se uma contagem puder separar os destinos, escreva a verificação "
        "(SQL só de agregado) em _controle/verificacoes/pendentes/ e cite o arquivo; senão registre em duvidas.md.",
        "Não conecte ao banco e não copie valores de dado nas justificativas.",
    ],
}


# ----------------------------------------------------------------- sanitização de código

_NUMERO_LONGO = re.compile(r"(?<![A-Za-z_$\d])\d{5,}")
_TAG_DOLAR = re.compile(r"\$[A-Za-z_]*\$")


def _numeros(texto: str) -> str:
    return _NUMERO_LONGO.sub("0", texto)


def _fim_string(texto: str, i: int) -> int:
    """Índice logo após a string que abre em `i` (aspas simples, com '' e, em E'...', com barra)."""
    escape = i > 0 and texto[i - 1] in "eE" and (i < 2 or not (texto[i - 2].isalnum() or texto[i - 2] == "_"))
    j = i + 1
    while j < len(texto):
        if escape and texto[j] == "\\":
            j += 2
            continue
        if texto[j] == "'":
            if texto[j + 1 : j + 2] == "'":
                j += 2
                continue
            return j + 1
        j += 1
    return len(texto)


def sanitizar_codigo(texto: str) -> str:
    """Strings viram `'?'` e números com mais de 4 dígitos viram `0`; comentários só passam pela regra dos números.

    O primeiro `$tag$` do texto é o delimitador do corpo da rotina e é mantido; outro `$tag$` abre string.
    """
    saida: list[str] = []
    buf: list[str] = []
    corpo: str | None = None
    i, n = 0, len(texto)

    def descarregar() -> None:
        if buf:
            saida.append(_numeros("".join(buf)))
            buf.clear()

    while i < n:
        c = texto[i]
        if texto.startswith("--", i):
            j = texto.find("\n", i)
            j = n if j < 0 else j
            buf.append(texto[i:j])
            i = j
        elif texto.startswith("/*", i):
            j = texto.find("*/", i + 2)
            j = n if j < 0 else j + 2
            buf.append(texto[i:j])
            i = j
        elif c == "'":
            descarregar()
            saida.append("'?'")
            i = _fim_string(texto, i)
        elif c == '"':
            j = i + 1
            while j < n:
                if texto[j] == '"':
                    if texto[j + 1 : j + 2] == '"':
                        j += 2
                        continue
                    break
                j += 1
            saida.append(texto[i : j + 1])
            i = j + 1
        elif c == "$" and (m := _TAG_DOLAR.match(texto, i)):
            tag = m.group(0)
            if corpo is None:
                corpo = tag
            if tag == corpo:
                buf.append(tag)
                i = m.end()
            else:
                descarregar()
                fim = texto.find(tag, m.end())
                saida.append("'?'")
                i = n if fim < 0 else fim + len(tag)
        else:
            buf.append(c)
            i += 1
    descarregar()
    return "".join(saida)


# ----------------------------------------------------------------- contexto de leitura


@dataclass
class Contexto:
    """Tudo o que a classificação e os lotes leem (metadado do catálogo mascarado, fatos e limiares)."""

    relacoes: dict[tuple[str, str], dict[str, Any]]
    funcoes: dict[tuple[str, str], list[dict[str, Any]]]
    linhas: dict[tuple[str, str], int | None]
    fatos: list[str]
    amostra_min: int

    def relacao(self, schema: str, nome: str) -> dict[str, Any]:
        return self.relacoes.get((schema, nome), {})


def ler_fatos(doc: Path) -> list[str]:
    """Textos dos fatos de `config/fatos.yml`; ignora `banco`, `credencial` e `tenant` (nunca lê valores)."""
    caminho = doc / "config" / "fatos.yml"
    if not caminho.is_file():
        return []
    try:
        dados = yaml.safe_load(caminho.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        raise ErroRelacionamentos(f"YAML inválido em {caminho}") from None
    fatos: list[str] = []
    for chave in sorted(dados) if isinstance(dados, dict) else []:
        valor = dados[chave]
        if chave in CHAVES_FATOS_IGNORADAS or not isinstance(valor, list):
            continue
        fatos.extend(str(f) for f in valor if isinstance(f, str))
    return fatos


def montar_contexto(doc: Path, amostra_min: int) -> Contexto:
    raiz = doc / "catalogo"
    estruturas = carregar_estruturas(raiz)
    estat = ler_estatisticas(raiz)
    filhos = mapa_de_particoes(estruturas)
    relacoes: dict[tuple[str, str], dict[str, Any]] = {}
    funcoes: dict[tuple[str, str], list[dict[str, Any]]] = {}
    linhas: dict[tuple[str, str], int | None] = {}
    for schema, d in estruturas.items():
        for rel in d.get("relacoes", []):
            chave = (schema, rel["nome"])
            relacoes[chave] = rel
            linhas[chave] = linhas_estimadas(chave, chave in filhos, estat, filhos)
        for fn in d.get("funcoes", []):
            funcoes.setdefault((schema, fn["nome"]), []).append(fn)
    return Contexto(relacoes, funcoes, linhas, ler_fatos(doc), amostra_min)


# ----------------------------------------------------------------- agrupamento e classificação


def chave_origem(a: dict[str, Any]) -> ChaveColuna:
    o = a["origem"]
    return (o["schema"], o["relacao"], tuple(o["colunas"]))


def id_coluna(chave: ChaveColuna) -> str:
    return f"{chave[0]}.{chave[1]}.{','.join(chave[2])}"


def eh_ambigua(a: dict[str, Any]) -> bool:
    return any(s.startswith(PREFIXO_AMBIGUO) for s in _segmentos(a.get("nota")))


def agrupar_ambiguas(arestas: Iterable[dict[str, Any]]) -> dict[ChaveColuna, list[dict[str, Any]]]:
    """Colunas de origem com pelo menos uma aresta ainda ambígua, com todas as arestas inferidas dela (por `id`)."""
    todas: dict[ChaveColuna, list[dict[str, Any]]] = {}
    for a in arestas:
        if a["fonte"] in FONTES_INFERIDAS:
            todas.setdefault(chave_origem(a), []).append(a)
    return {
        k: sorted(v, key=lambda a: a["id"]) for k, v in sorted(todas.items()) if any(eh_ambigua(a) for a in v)
    }


def amostragem_de(a: dict[str, Any]) -> dict[str, Any] | None:
    achadas = [e for e in a["evidencia"] if e.get("tipo") == "amostragem"]
    return achadas[-1] if achadas else None


def tem_taxa_util(a: dict[str, Any], amostra_min: int) -> bool:
    am = amostragem_de(a)
    return a["taxa_casamento"] is not None and am is not None and am.get("amostrados", 0) >= amostra_min


def _cita(texto: str | None, nome: str) -> bool:
    if not texto or not nome:
        return False
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(nome)}(?![A-Za-z0-9_])", texto, re.IGNORECASE) is not None


def _nomes_destino(a: dict[str, Any]) -> list[str]:
    d = a["destino"]
    return [d["relacao"], f"{d['schema']}.{d['relacao']}"]


def _comentarios_origem(chave: ChaveColuna, ctx: Contexto) -> list[str]:
    rel = ctx.relacao(chave[0], chave[1])
    textos = [rel.get("comentario")]
    textos += [c.get("comentario") for c in rel.get("colunas", []) if c["nome"] in chave[2]]
    return [t for t in textos if isinstance(t, str)]


def fatos_da_coluna(chave: ChaveColuna, arestas: list[dict[str, Any]], ctx: Contexto) -> list[str]:
    """Fatos de `fatos.yml` que citam uma coluna de origem ou um destino (nome da relação ou `schema.relacao`)."""
    nomes = [*chave[2]] + [n for a in arestas for n in _nomes_destino(a)]
    return [f for f in ctx.fatos if any(_cita(f, n) for n in nomes)]


def motivos_p2(chave: ChaveColuna, arestas: list[dict[str, Any]], ctx: Contexto) -> list[str]:
    motivos = []
    if any(a["fonte"] == FONTE_CODIGO for a in arestas):
        motivos.append("codigo")
    destinos = [n for a in arestas for n in _nomes_destino(a)]
    if any(_cita(t, n) for t in _comentarios_origem(chave, ctx) for n in destinos):
        motivos.append("comentario")
    if fatos_da_coluna(chave, arestas, ctx):
        motivos.append("fato")
    return motivos


def motivo_p3(chave: ChaveColuna, arestas: list[dict[str, Any]], ctx: Contexto) -> str:
    """Motivo predominante da falta de evidência (só para contagem agregada)."""
    notas = " | ".join(a.get("nota") or "" for a in arestas)
    if ctx.linhas.get((chave[0], chave[1])) == 0 or "amostra vazia" in notas:
        return "origem_vazia"
    if "sem permissão de leitura" in notas:
        return "sem_select"
    if "amostra pequena" in notas:
        return "amostra_pequena"
    return "sem_verificacao"


def classificar(
    grupos: dict[ChaveColuna, list[dict[str, Any]]], ctx: Contexto
) -> dict[str, list[ChaveColuna]]:
    """`p1`, `p2` e `p3`: cada nível ordenado por linhas estimadas da origem (maior primeiro) e depois por `id`."""
    niveis: dict[str, list[ChaveColuna]] = {"p1": [], "p2": [], "p3": []}
    for chave, arestas in grupos.items():
        if any(tem_taxa_util(a, ctx.amostra_min) for a in arestas):
            niveis["p1"].append(chave)
        elif motivos_p2(chave, arestas, ctx):
            niveis["p2"].append(chave)
        else:
            niveis["p3"].append(chave)

    def ordem(chave: ChaveColuna) -> tuple[int, str]:
        linhas = ctx.linhas.get((chave[0], chave[1]))
        return (-(linhas if linhas is not None else -1), id_coluna(chave))

    return {n: sorted(v, key=ordem) for n, v in niveis.items()}


# ----------------------------------------------------------------- conteúdo da coluna


def _colunas_info(rel: dict[str, Any], nomes: Iterable[str]) -> list[dict[str, Any]]:
    por_nome = {c["nome"]: c for c in rel.get("colunas", [])}
    info = []
    for n in nomes:
        c = por_nome.get(n, {})
        info.append({"nome": n, "tipo": c.get("tipo"), "comentario": c.get("comentario")})
    return info


def _pk(rel: dict[str, Any]) -> list[str]:
    for r in rel.get("restricoes", []):
        if r.get("tipo") == "pk":
            return list(r["colunas"])
    return []


def _tabela_info(ctx: Contexto, schema: str, nome: str) -> dict[str, Any]:
    rel = ctx.relacao(schema, nome)
    return {
        "schema": schema,
        "relacao": nome,
        "tipo": rel.get("tipo"),
        "comentario": rel.get("comentario"),
        "linhas_estimadas": ctx.linhas.get((schema, nome)),
        "pk": _pk(rel),
    }


def _texto_do_objeto(ctx: Contexto, obj: dict[str, Any]) -> list[str]:
    """Textos (`definicao` de view ou `codigo` de rotina) do objeto citado na evidência de código."""
    if obj["tipo"] in ("view", "materializada"):
        d = ctx.relacao(obj["schema"], obj["nome"]).get("definicao")
        return [d] if isinstance(d, str) else []
    return [f["codigo"] for f in ctx.funcoes.get((obj["schema"], obj["nome"]), []) if isinstance(f.get("codigo"), str)]


def trecho(texto: str, nomes: list[str]) -> list[str] | None:
    """Até `MAX_LINHAS` linhas, já sanitizadas, em volta da primeira linha que cita um dos nomes."""
    linhas = sanitizar_codigo(texto).splitlines()
    for nome in nomes:
        for i, linha in enumerate(linhas):
            if _cita(linha, nome):
                ini = max(0, min(i - MAX_LINHAS // 2, len(linhas) - MAX_LINHAS))
                return [ln.rstrip()[:MAX_CHARS_LINHA] for ln in linhas[ini : ini + MAX_LINHAS]]
    return None


def trechos_do_destino(a: dict[str, Any], chave: ChaveColuna, ctx: Contexto) -> list[dict[str, Any]]:
    objetos = [o for e in a["evidencia"] if e.get("tipo") == FONTE_CODIGO for o in e.get("objetos", [])]
    achados: list[dict[str, Any]] = []
    for obj in objetos:
        if len(achados) >= MAX_OBJETOS:
            break
        for texto in _texto_do_objeto(ctx, obj):
            t = trecho(texto, [*chave[2], a["destino"]["relacao"]])
            if t:
                achados.append({"objeto": dict(obj), "linhas": t})
                break
    return achados


def _destino_info(a: dict[str, Any], chave: ChaveColuna, ctx: Contexto, com_trechos: bool) -> dict[str, Any]:
    d = a["destino"]
    nome_ev = next((e for e in a["evidencia"] if e.get("tipo") == FONTE_NOME), {})
    cod_ev = next((e for e in a["evidencia"] if e.get("tipo") == FONTE_CODIGO), {})
    info: dict[str, Any] = {
        "aresta": a["id"],
        "fonte": a["fonte"],
        "confianca": a["confianca"],
        "nota": a["nota"],
        "taxa_casamento": a["taxa_casamento"],
        "amostragem": amostragem_de(a),
        "regra_nome": nome_ev.get("regra"),
        "pontuacao_nome": nome_ev.get("pontuacao"),
        "objetos_codigo": cod_ev.get("objetos", []),
        "total_objetos_codigo": cod_ev.get("total_objetos", 0),
        "destino": {
            "schema": d["schema"],
            "relacao": d["relacao"],
            "colunas": _colunas_info(ctx.relacao(d["schema"], d["relacao"]), d["colunas"]),
        },
        "tabela_destino": _tabela_info(ctx, d["schema"], d["relacao"]),
    }
    if com_trechos:
        info["trechos"] = trechos_do_destino(a, chave, ctx)
    return info


def montar_coluna(
    chave: ChaveColuna, arestas: list[dict[str, Any]], nivel: str, ctx: Contexto, com_trechos: bool = True
) -> dict[str, Any]:
    rel = ctx.relacao(chave[0], chave[1])
    motivos = ["taxa_util"] if nivel == "p1" else motivos_p2(chave, arestas, ctx)
    return {
        "id_coluna": id_coluna(chave),
        "nivel": nivel,
        "motivos": motivos,
        "origem": {
            "tabela": _tabela_info(ctx, chave[0], chave[1]),
            "colunas": _colunas_info(rel, chave[2]),
        },
        "destinos": [_destino_info(a, chave, ctx, com_trechos) for a in arestas],
        "fatos": fatos_da_coluna(chave, arestas, ctx),
    }


def tamanho(obj: Any) -> int:
    return len(serializar_json(obj).encode("utf-8"))


def peso_na_lista(coluna: dict[str, Any]) -> int:
    """Bytes que a coluna ocupa dentro de `colunas` do lote (a indentação conta)."""
    return tamanho({"colunas": [coluna]}) - tamanho({"colunas": []})


def montar_coluna_limitada(
    chave: ChaveColuna, arestas: list[dict[str, Any]], nivel: str, ctx: Contexto
) -> dict[str, Any]:
    """Se a coluna sozinha passa do limite, os trechos de código dela são cortados primeiro."""
    coluna = montar_coluna(chave, arestas, nivel, ctx)
    if peso_na_lista(coluna) > MAX_BYTES - FOLGA_CABECALHO:
        coluna = montar_coluna(chave, arestas, nivel, ctx, com_trechos=False)
    return coluna


# ----------------------------------------------------------------- lotes


def dividir(colunas: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Lotes de até `MAX_COLUNAS` colunas e `MAX_BYTES` bytes, preservando a ordem."""
    lotes: list[list[dict[str, Any]]] = []
    atual: list[dict[str, Any]] = []
    bytes_atuais = FOLGA_CABECALHO
    for c in colunas:
        t = peso_na_lista(c)
        if atual and (len(atual) >= MAX_COLUNAS or bytes_atuais + t > MAX_BYTES):
            lotes.append(atual)
            atual, bytes_atuais = [], FOLGA_CABECALHO
        atual.append(c)
        bytes_atuais += t
    if atual:
        lotes.append(atual)
    return lotes


def _nome_lote(lote: str) -> str:
    return f"relacionamentos-{lote}.json"


def _nome_resolvido(lote: str) -> str:
    return f"relacionamentos-{lote}.resolvido.json"


def lotes_existentes(pasta: Path) -> dict[str, Path]:
    """Lotes já gravados, por id."""
    achados: dict[str, Path] = {}
    for arquivo in sorted(pasta.glob("relacionamentos-*.json")) if pasta.is_dir() else []:
        m = PADRAO_LOTE.match(arquivo.name)
        if m:
            achados[m.group(1)] = arquivo
    return achados


def lotes_resolvidos(pasta: Path) -> dict[str, list[str]]:
    """Lotes com `.resolvido.json`: id e colunas que eles cobriram (lidas do arquivo do lote)."""
    resolvidos: dict[str, list[str]] = {}
    for lote, arquivo in lotes_existentes(pasta).items():
        if not (pasta / _nome_resolvido(lote)).is_file():
            continue
        try:
            dados = json.loads(arquivo.read_text(encoding="utf-8"))
            resolvidos[lote] = [c["id_coluna"] for c in dados["colunas"]]
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            resolvidos[lote] = []
    return resolvidos


def _proximo_id(nivel: str, usados: set[str], contador: list[int]) -> str:
    while True:
        contador[0] += 1
        lote = f"{nivel}-{contador[0]:03d}"
        if lote not in usados:
            return lote


def gerar(doc: Path, amostra_min: int, refazer: bool = False) -> dict[str, Any]:
    """Grava lotes, índice e lista P3; devolve o índice. Lote já resolvido não é sobrescrito (salvo `refazer`)."""
    caminho = doc.joinpath(*SAIDA)
    if not caminho.is_file():
        raise ErroRelacionamentos(f"{caminho} não existe. Rode 'relacionamentos fks' antes.")
    grafo = ler_grafo_existente(caminho)
    ctx = montar_contexto(doc, amostra_min)
    pasta = doc.joinpath(*PASTA_LOTES)
    resolvidos = {} if refazer else lotes_resolvidos(pasta)
    ja_cobertas = {c for cols in resolvidos.values() for c in cols}

    grupos = agrupar_ambiguas(grafo["arestas"])
    niveis = classificar(grupos, ctx)
    plano: dict[str, list[list[dict[str, Any]]]] = {}
    for nivel in NIVEIS:
        colunas = [
            montar_coluna_limitada(k, grupos[k], nivel, ctx)
            for k in niveis[nivel]
            if id_coluna(k) not in ja_cobertas
        ]
        plano[nivel] = dividir(colunas)

    pasta.mkdir(parents=True, exist_ok=True)
    entradas: list[dict[str, Any]] = []
    novos: set[str] = set()
    for nivel in NIVEIS:
        reservados = {l for l in resolvidos if l.startswith(nivel)}
        contador = [0]
        for bloco in plano[nivel]:
            lote = _proximo_id(nivel, reservados | novos, contador)
            novos.add(lote)
            documento = {
                "formato": "relacionamentos_lote",
                "versao_formato": 1,
                "lote": lote,
                "nivel": nivel,
                "formato_saida": FORMATO_SAIDA,
                "colunas": bloco,
            }
            escrever_json(pasta / _nome_lote(lote), documento)
            entradas.append({"lote": lote, "nivel": nivel, "colunas": len(bloco), "situacao": "pendente"})
    for lote, arquivo in lotes_existentes(pasta).items():  # pendentes antigos que saíram do plano
        if lote not in novos and lote not in resolvidos:
            arquivo.unlink()
    for lote in sorted(resolvidos):
        qtd = len(resolvidos[lote])
        entradas.append({"lote": lote, "nivel": lote[:2], "colunas": qtd, "situacao": "resolvido"})

    p3 = niveis["p3"]
    motivos = Counter(motivo_p3(k, grupos[k], ctx) for k in p3)
    fila = {n: sum(id_coluna(k) not in ja_cobertas for k in niveis[n]) for n in NIVEIS}
    for lote, cols in resolvidos.items():
        fila[lote[:2]] += len(cols)
    indice = {
        "formato": "relacionamentos_indice",
        "versao_formato": 1,
        "lotes": sorted(entradas, key=lambda e: e["lote"]),
        "resumo": {
            "colunas_fila": {"p1": fila["p1"], "p2": fila["p2"], "p3": len(p3)},
            "p3_motivos": dict(sorted(motivos.items())),
        },
    }
    escrever_json(pasta / ARQ_INDICE, indice)
    escrever_json(doc.joinpath(*SAIDA_SEM_EVIDENCIA), lista_p3(p3, grupos, motivos_de(p3, grupos, ctx)))
    return indice


def motivos_de(
    p3: list[ChaveColuna], grupos: dict[ChaveColuna, list[dict[str, Any]]], ctx: Contexto
) -> dict[ChaveColuna, str]:
    return {k: motivo_p3(k, grupos[k], ctx) for k in p3}


def lista_p3(
    p3: list[ChaveColuna], grupos: dict[ChaveColuna, list[dict[str, Any]]], motivos: dict[ChaveColuna, str]
) -> dict[str, Any]:
    """Só nomes de objeto: coluna de origem, destinos possíveis e motivo predominante."""
    itens = []
    for k in p3:
        destinos = sorted(
            {(a["destino"]["schema"], a["destino"]["relacao"], tuple(a["destino"]["colunas"])) for a in grupos[k]}
        )
        itens.append(
            {
                "id_coluna": id_coluna(k),
                "origem": {"schema": k[0], "relacao": k[1], "colunas": list(k[2])},
                "destinos": [{"schema": s, "relacao": r, "colunas": list(c)} for s, r, c in destinos],
                "motivo": motivos[k],
            }
        )
    return {"formato": "relacionamentos_sem_evidencia", "versao_formato": 1, "total": len(itens), "colunas": itens}


# ----------------------------------------------------------------- CLI


def registrar(etapas: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = etapas.add_parser("lotes", help="classifica as colunas ambíguas e gera os lotes do analista (offline)")
    adicionar_argumentos_comuns(p)
    p.add_argument("--refazer", action="store_true", help="regera também os lotes já resolvidos")
    p.set_defaults(func=executar)


def executar(args: argparse.Namespace) -> int:
    projeto = carregar_projeto(args.doc, args.workspace)
    indice = gerar(projeto.doc, projeto.amostra_min_valores, refazer=args.refazer)
    fila = indice["resumo"]["colunas_fila"]
    lotes = indice["lotes"]
    print(f"Fila do analista: P1={fila['p1']}, P2={fila['p2']}, P3={fila['p3']} (sem evidência, só lista).")
    por_nivel = Counter(l["nivel"] for l in lotes)
    pendentes = sum(l["situacao"] == "pendente" for l in lotes)
    print(f"Lotes: {len(lotes)} (p1={por_nivel['p1']}, p2={por_nivel['p2']}); pendentes={pendentes}.")
    pasta = projeto.doc.joinpath(*PASTA_LOTES)
    tamanhos = [(pasta / _nome_lote(l["lote"])).stat().st_size for l in lotes if (pasta / _nome_lote(l["lote"])).is_file()]
    if tamanhos:
        print(f"Tamanho dos lotes: máximo {max(tamanhos) // 1024} KB, total {sum(tamanhos) // 1024} KB.")
    print("Motivos P3: " + ", ".join(f"{m}={n}" for m, n in indice["resumo"]["p3_motivos"].items()) + ".")
    return 0
