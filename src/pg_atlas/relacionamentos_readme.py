"""Seções de relacionamentos dos `schemas/<schema>/README.md` (tarefa 2.7, decisão D-022).

Duas seções automáticas, entre marcadores `pg-atlas:auto`:
- `grupos`: lotes que contêm tabelas do schema e diagramas Mermaid dos grupos principais;
- `ligacoes`: contagem de arestas com os outros schemas.

Cada seção ocupa o lugar do marcador de pendência da fase 2 (`Grupos de tabelas`, `Ligações com outros
schemas`); se já existe, só o miolo é reescrito; se nenhum dos dois existe, vai para o fim do arquivo.
O texto fora dos marcadores nunca é tocado. A saída é determinística e só leva nomes e contagens.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pg_atlas.esqueleto_formato import milhar, tabela
from pg_atlas.esqueleto_marcadores import envolver, interpretar, neutralizar
from pg_atlas.esqueleto_nomes import caminho_readme
from pg_atlas.relacionamentos import ErroRelacionamentos
from pg_atlas.triagem import escrever_texto

GRUPOS_PRINCIPAIS = 5
LINHAS_TABELA = 12
MAX_NOS = 12
MAX_ARESTAS = 30
MAX_OUTROS_SCHEMAS = 15
MARCA_PENDENTE = '<!-- pg-atlas:pendente fase="2" -->'
Chave = tuple[str, str]
Pares = dict[tuple[Chave, Chave], str]  # (origem, destino) -> melhor confiança


def pares_do_grafo(grafo: dict[str, Any], confiancas: set[str]) -> Pares:
    """Pares (origem, destino) de tabela com a melhor confiança entre as usadas."""
    ordem = {"declarado": 0, "confirmado": 1, "hipotese": 2}
    pares: Pares = {}
    for a in grafo["arestas"]:
        if a["confianca"] not in confiancas:
            continue
        par = ((a["origem"]["schema"], a["origem"]["relacao"]), (a["destino"]["schema"], a["destino"]["relacao"]))
        if par[0] == par[1]:
            continue
        if par not in pares or ordem[a["confianca"]] < ordem[pares[par]]:
            pares[par] = a["confianca"]
    return pares


# ----------------------------------------------------------------- diagrama


def _rotulo(texto: str) -> str:
    return texto.replace('"', "#quot;")


def diagrama(tabelas: list[Chave], pares: Pares) -> tuple[str, int, int] | None:
    """Mermaid `flowchart` do grupo (só nomes); None se não há aresta entre as tabelas.

    Devolve (texto, nós desenhados, nós do grupo com aresta). No máximo `MAX_NOS` nós (os de maior grau)
    e `MAX_ARESTAS` arestas.
    """
    conjunto = set(tabelas)
    arestas = sorted((o, d, c) for (o, d), c in pares.items() if o in conjunto and d in conjunto)
    if not arestas:
        return None
    grau: dict[Chave, int] = {}
    for o, d, _ in arestas:
        grau[o] = grau.get(o, 0) + 1
        grau[d] = grau.get(d, 0) + 1
    escolhidos = sorted(sorted(grau, key=lambda n: (-grau[n], n))[:MAX_NOS])
    ids = {n: f"t{i}" for i, n in enumerate(escolhidos, start=1)}
    visiveis = [(o, d, c) for o, d, c in arestas if o in ids and d in ids][:MAX_ARESTAS]
    linhas = ["flowchart LR"]
    linhas += [f'    {ids[n]}["{_rotulo(n[1])}"]' for n in escolhidos]
    linhas += [f"    {ids[o]} {'-.->' if c == 'confirmado' else '-->'} {ids[d]}" for o, d, c in visiveis]
    return "```mermaid\n" + "\n".join(linhas) + "\n```", len(escolhidos), len(grau)


# ----------------------------------------------------------------- seções


def _lotes_do_schema(schema: str, lotes: dict[str, Any]) -> list[tuple[dict[str, Any], list[Chave]]]:
    achados = []
    for lote in lotes["lotes"]:
        aqui = sorted((t["schema"], t["nome"]) for t in lote["tabelas"] if t["schema"] == schema)
        if aqui:
            achados.append((lote, aqui))
    return sorted(achados, key=lambda x: (-len(x[1]), x[0]["id"]))


def _outros_schemas(lote: dict[str, Any], schema: str) -> str:
    outros = [s for s in lote["schemas"] if s != schema]
    if not outros:
        return "—"
    texto = ", ".join(f"`{s}`" for s in outros[:3])
    return texto + (f" e mais {len(outros) - 3}" if len(outros) > 3 else "")


def secao_grupos(schema: str, lotes: dict[str, Any], pares: Pares) -> str:
    p = lotes["parametros"]
    achados = _lotes_do_schema(schema, lotes)
    cabeca = [
        "## Grupos de tabelas",
        "",
        "Gerado de `relacionamentos/lotes.json`: comunidades do grafo de relacionamentos "
        f"(arestas {', '.join(p['confiancas_usadas'])}; até {p['max_tabelas']} tabelas por lote). "
        "Seta cheia: FK declarada; pontilhada: relação confirmada por amostragem.",
    ]
    if not achados:
        return "\n".join([*cabeca, "", "Nenhuma tabela deste schema entrou em lote."])
    grafo_lotes = [x for x in achados if x[0]["tipo"] == "grafo"]
    leves = [x for x in achados if x[0]["tipo"] == "leve"]
    linhas = [
        (lote["id"], milhar(len(aqui)), milhar(lote["total_tabelas"]), _outros_schemas(lote, schema))
        for lote, aqui in achados[:LINHAS_TABELA]
    ]
    saida = [*cabeca, "", tabela(["Lote", "Tabelas neste schema", "Tabelas no lote", "Outros schemas"], linhas)]
    if len(achados) > LINHAS_TABELA:
        saida += ["", f"E mais {len(achados) - LINHAS_TABELA} lotes menores."]
    if leves:
        por_classe: dict[str, list[int]] = {}
        for lote, aqui in leves:
            por_classe.setdefault(lote["classe_leve"], []).append(len(aqui))
        partes = [f"{milhar(sum(n))} `{c}` em {len(n)} lote(s)" for c, n in sorted(por_classe.items())]
        saida += ["", "Tabelas em lotes leves (só descrição e etiqueta na fase 3): " + "; ".join(partes) + "."]
    saida += _grupos_principais(grafo_lotes, pares)
    return "\n".join(saida)


def _grupos_principais(grafo_lotes: list[tuple[dict[str, Any], list[Chave]]], pares: Pares) -> list[str]:
    saida: list[str] = []
    mostrados = 0
    for lote, aqui in grafo_lotes:
        if mostrados == GRUPOS_PRINCIPAIS:
            break
        desenho = diagrama(aqui, pares)
        if desenho is None:
            continue
        texto, desenhados, com_aresta = desenho
        mostrados += 1
        nota = f" (mostrando {desenhados} de {com_aresta} com relação)" if desenhados < com_aresta else ""
        saida += ["", f"### {lote['id']}", "", f"{len(aqui)} tabelas deste schema{nota}.", "", texto]
    return saida


def secao_ligacoes(schema: str, pares: Pares) -> str:
    saindo: dict[str, int] = {}
    entrando: dict[str, int] = {}
    for (o, d), _ in pares.items():
        if o[0] == schema and d[0] != schema:
            saindo[d[0]] = saindo.get(d[0], 0) + 1
        elif d[0] == schema and o[0] != schema:
            entrando[o[0]] = entrando.get(o[0], 0) + 1
    outros = sorted(set(saindo) | set(entrando), key=lambda s: (-(saindo.get(s, 0) + entrando.get(s, 0)), s))
    cabeca = ["## Ligações com outros schemas", ""]
    if not outros:
        return "\n".join([*cabeca, "Nenhuma ligação com outros schemas no grafo (arestas usadas no agrupamento)."])
    linhas = [(f"`{s}`", milhar(saindo.get(s, 0)), milhar(entrando.get(s, 0))) for s in outros[:MAX_OUTROS_SCHEMAS]]
    corpo = [
        "Pares de tabelas ligados (origem para destino), nas arestas usadas no agrupamento. "
        f"Este schema aponta para o outro ('Saindo') ou é apontado por ele ('Entrando'). Os {MAX_OUTROS_SCHEMAS} "
        "schemas com mais ligações.",
        "",
        tabela(["Outro schema", "Saindo", "Entrando"], linhas),
    ]
    if len(outros) > MAX_OUTROS_SCHEMAS:
        corpo += ["", f"E mais {len(outros) - MAX_OUTROS_SCHEMAS} schemas com menos ligações."]
    return "\n".join([*cabeca, *corpo])


# ----------------------------------------------------------------- aplicação ao arquivo


def aplicar_secao(texto: str, nome: str, conteudo: str, titulo: str) -> str:
    """Troca o miolo da seção, ou ocupa o lugar da pendência da fase 2, ou vai para o fim."""
    linhas = texto.split("\n")
    bloco = envolver(nome, neutralizar(conteudo)).split("\n")
    posicoes = interpretar(linhas)
    if nome in posicoes:
        ini, fim = posicoes[nome]
        linhas[ini : fim + 1] = bloco
    else:
        for i in range(len(linhas) - 3):
            if (
                linhas[i] == f"## {titulo}" and linhas[i + 1] == "" and linhas[i + 2] == MARCA_PENDENTE
                and linhas[i + 3].startswith("_Pendente")
            ):
                linhas[i : i + 4] = bloco
                break
        else:
            while linhas and linhas[-1] == "":
                linhas.pop()
            linhas += ["", *bloco, ""] if linhas else [*bloco, ""]
    resultado = "\n".join(linhas)
    return resultado if resultado.endswith("\n") else resultado + "\n"


def atualizar_readmes(doc: Path, lotes: dict[str, Any], grafo: dict[str, Any]) -> dict[str, int]:
    """Atualiza o README de cada schema do catálogo. Falta de README ou marcador quebrado é erro."""
    schemas = sorted(a.stem for a in (doc / "catalogo" / "estrutura").glob("*.json"))
    pares = pares_do_grafo(grafo, set(lotes["parametros"]["confiancas_usadas"]))
    novos: dict[Path, str] = {}
    contagem = {"atualizados": 0, "inalterados": 0}
    for schema in schemas:
        destino = doc / caminho_readme(schema)
        if not destino.is_file():
            raise ErroRelacionamentos(f"README ausente: {destino}. Rode 'pg_atlas esqueleto'.")
        atual = destino.read_text(encoding="utf-8-sig")
        novo = aplicar_secao(atual, "grupos", secao_grupos(schema, lotes, pares), "Grupos de tabelas")
        novo = aplicar_secao(novo, "ligacoes", secao_ligacoes(schema, pares), "Ligações com outros schemas")
        if novo != atual or destino.read_bytes().startswith(b"\xef\xbb\xbf"):
            novos[destino] = novo
        else:
            contagem["inalterados"] += 1
    for destino, texto in novos.items():  # só grava se todos os marcadores estavam íntegros
        escrever_texto(destino, texto)
    contagem["atualizados"] = len(novos)
    return contagem
