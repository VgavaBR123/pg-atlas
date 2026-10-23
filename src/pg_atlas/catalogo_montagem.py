"""Montagem pura (sem banco) dos documentos do catálogo a partir das linhas das consultas.

Entrada: para cada consulta de `sql/catalogo/`, uma lista de dicionários (coluna -> valor).
Saída: dicionários prontos para gravar com `json_util.escrever_json`. Tudo que é volátil
(linhas, tamanhos, datas de analyze, scans, estatísticas de coluna) vai só para o documento
de estatísticas; a estrutura é determinística entre extrações.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from importlib import resources
from typing import Any

import jsonschema

from pg_atlas.erros import ErroAtlas

VERSAO_FORMATO = 1
FORMATOS = ("banco", "estatisticas", "estrutura", "mascaramento")
Linha = dict[str, Any]
Linhas = dict[str, list[Linha]]

TIPOS_RELACAO = {
    "r": "tabela",
    "p": "particionada",
    "v": "view",
    "m": "materializada",
    "f": "estrangeira",
}
TIPOS_REFERENCIA = {**TIPOS_RELACAO, "c": "composto", "S": "sequencia"}
TIPOS_RESTRICAO = {"p": "pk", "u": "uk", "f": "fk", "c": "check"}
ACOES_FK = {"a": "no_action", "r": "restrict", "c": "cascade", "n": "set_null", "d": "set_default"}
TIPOS_MATCH = {"f": "full", "p": "partial", "s": "simple"}
IDENTIDADES = {"a": "sempre", "d": "padrao"}
VOLATILIDADES = {"i": "imutavel", "s": "estavel", "v": "volatil"}
HABILITACOES = {"O": "origem", "D": "desabilitado", "R": "replica", "A": "sempre"}
PERSISTENCIAS = {"p": "permanente", "u": "unlogged", "t": "temporaria"}
CAMPOS_ESTATISTICA_RELACAO = (
    "linhas_estimadas",
    "paginas",
    "tamanho_total_bytes",
    "tamanho_tabela_bytes",
    "ultimo_analyze",
    "ultimo_autoanalyze",
    "ultimo_vacuum",
    "ultimo_autovacuum",
    "seq_scans",
    "linhas_lidas_seq",
    "idx_scans",
    "linhas_vivas",
    "linhas_mortas",
)
# De pg_stats só estas quatro (regra inviolável 4).
CAMPOS_ESTATISTICA_COLUNA = ("null_frac", "n_distinct", "avg_width", "correlation")


class ErroValidacaoCatalogo(ErroAtlas):
    """Documento do catálogo fora do formato definido em `catalogo.schema.json`."""

    codigo_saida = 4


# ----------------------------------------------------------------- utilidades


def json_seguro(valor: Any) -> Any:
    """Converte valores do banco (datas, Decimal, tuplas) em tipos JSON, recursivamente."""
    if isinstance(valor, (datetime, date)):
        return valor.isoformat()
    if isinstance(valor, Decimal):
        return int(valor) if valor == valor.to_integral_value() else float(valor)
    if isinstance(valor, (list, tuple)):
        return [json_seguro(v) for v in valor]
    if isinstance(valor, dict):
        return {str(k): json_seguro(v) for k, v in valor.items()}
    return valor


def _mapa(tabela: dict[str, str], codigo: Any) -> str | None:
    if codigo in (None, ""):
        return None
    return tabela.get(str(codigo), str(codigo))


def _lista(valor: Any) -> list[Any]:
    return [] if valor is None else list(json_seguro(valor))


def _por(linhas: list[Linha], *chaves: str) -> dict[tuple[Any, ...], list[Linha]]:
    agrupado: dict[tuple[Any, ...], list[Linha]] = {}
    for linha in linhas:
        agrupado.setdefault(tuple(linha.get(c) for c in chaves), []).append(linha)
    return agrupado


def _texto(valor: Any) -> str:
    return "" if valor is None else str(valor)


def _ordenar(itens: list[dict[str, Any]], *chaves: str) -> list[dict[str, Any]]:
    return sorted(itens, key=lambda i: tuple(_texto(i[c]) for c in chaves))


# ----------------------------------------------------------------- estrutura


def _coluna(l: Linha) -> dict[str, Any]:
    return {
        "ordem": int(l["ordem"]),
        "nome": l["nome"],
        "tipo": l["tipo"],
        "obrigatoria": bool(l["obrigatoria"]),
        "padrao": l.get("padrao"),
        "identidade": _mapa(IDENTIDADES, l.get("identidade")),
        "gerada": "armazenada" if l.get("gerada") == "s" else None,
        "expressao_gerada": l.get("expressao_gerada"),
        "comentario": l.get("comentario"),
    }


def _restricao(l: Linha) -> dict[str, Any]:
    fk = l["tipo"] == "f"
    return {
        "nome": l["nome"],
        "tipo": _mapa(TIPOS_RESTRICAO, l["tipo"]),
        "colunas": _lista(l.get("colunas")),
        "ref_schema": l.get("ref_schema") if fk else None,
        "ref_relacao": l.get("ref_relacao") if fk else None,
        "ref_colunas": _lista(l.get("ref_colunas")) if fk else [],
        "acao_delete": _mapa(ACOES_FK, l.get("acao_delete")) if fk else None,
        "acao_update": _mapa(ACOES_FK, l.get("acao_update")) if fk else None,
        "tipo_match": _mapa(TIPOS_MATCH, l.get("tipo_match")) if fk else None,
        "adiavel": bool(l.get("adiavel")),
        "adiada": bool(l.get("adiada")),
        "validada": bool(l.get("validada")),
        "herdada": bool(l.get("herdada")),
        "definicao": l.get("definicao"),
    }


def _indice(l: Linha) -> dict[str, Any]:
    return {
        "nome": l["nome"],
        "metodo": l.get("metodo"),
        "colunas": _lista(l.get("colunas")),
        "unico": bool(l.get("unico")),
        "primario": bool(l.get("primario")),
        "parcial": bool(l.get("parcial")),
        "predicado": l.get("predicado"),
        "valido": bool(l.get("valido")),
        "definicao": l.get("definicao"),
    }


def _gatilho(l: Linha) -> dict[str, Any]:
    return {
        "nome": l["nome"],
        "eventos": sorted(_lista(l.get("eventos"))),
        "momento": l.get("momento"),
        "nivel": l.get("nivel"),
        "habilitado": _mapa(HABILITACOES, l.get("habilitado")),
        "funcao_schema": l.get("funcao_schema"),
        "funcao_nome": l.get("funcao_nome"),
        "definicao": l.get("definicao"),
    }


def _dependencias(linhas: list[Linha]) -> dict[str, list[dict[str, Any]]]:
    saida: dict[str, list[dict[str, Any]]] = {}
    for l in linhas:
        saida.setdefault(l["view"], []).append(
            {
                "schema": l["ref_schema"],
                "relacao": l["ref_relacao"],
                "tipo": _mapa(TIPOS_REFERENCIA, l.get("ref_tipo")),
            }
        )
    return {k: _ordenar(v, "schema", "relacao") for k, v in saida.items()}


def _relacao(
    l: Linha,
    filhos: dict[str, dict[tuple[Any, ...], list[Linha]]],
    acessos: dict[str, str],
    views: dict[str, Linha],
    deps: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    nome = l["nome"]
    chave = (l["schema"], nome)
    particao = None
    if l.get("eh_particao"):
        particao = {
            "pai_schema": l.get("pai_schema"),
            "pai_nome": l.get("pai_nome"),
            "limite": l.get("limite_particao"),
        }
    visao = views.get(nome)
    colunas = [_coluna(x) for x in filhos["colunas"].get(chave, [])]
    return {
        "nome": nome,
        "tipo": _mapa(TIPOS_RELACAO, l["tipo"]),
        "dono": l.get("dono"),
        "persistencia": _mapa(PERSISTENCIAS, l.get("persistencia")),
        "comentario": l.get("comentario"),
        "acesso": acessos.get(nome, "ok"),
        "particao": particao,
        "chave_particao": l.get("chave_particao"),
        "populada": visao.get("populada") if visao and l["tipo"] == "m" else None,
        "definicao": visao.get("definicao") if visao else None,
        "depende_de": deps.get(nome, []),
        "colunas": sorted(colunas, key=lambda c: (c["ordem"], c["nome"])),
        "restricoes": _ordenar(
            [_restricao(x) for x in filhos["restricoes"].get(chave, [])], "nome"
        ),
        "indices": _ordenar([_indice(x) for x in filhos["indices"].get(chave, [])], "nome"),
        "gatilhos": _ordenar([_gatilho(x) for x in filhos["gatilhos"].get(chave, [])], "nome"),
    }


def _funcao(l: Linha, acessos: dict[tuple[str, str], str]) -> dict[str, Any]:
    return {
        "nome": l["nome"],
        "tipo": l.get("tipo"),
        "linguagem": l.get("linguagem"),
        "argumentos": l.get("argumentos"),
        "argumentos_identidade": l.get("argumentos_identidade"),
        "retorno": l.get("retorno"),
        "volatilidade": _mapa(VOLATILIDADES, l.get("volatilidade")),
        "security_definer": bool(l.get("security_definer")),
        "estrita": bool(l.get("estrita")),
        "dono": l.get("dono"),
        "comentario": l.get("comentario"),
        "acesso": acessos.get((l["nome"], _texto(l.get("argumentos_identidade"))), "ok"),
        "codigo": l.get("codigo"),
    }


def _sequencia(l: Linha) -> dict[str, Any]:
    tem_coluna = l.get("coluna_nome") is not None
    return {
        "nome": l["nome"],
        "tipo": l.get("tipo"),
        "inicio": json_seguro(l.get("inicio")),
        "minimo": json_seguro(l.get("minimo")),
        "maximo": json_seguro(l.get("maximo")),
        "incremento": json_seguro(l.get("incremento")),
        "ciclica": bool(l.get("ciclica")),
        "cache": json_seguro(l.get("cache")),
        "dono": l.get("dono"),
        "coluna_schema": l.get("coluna_schema") if tem_coluna else None,
        "coluna_relacao": l.get("coluna_relacao") if tem_coluna else None,
        "coluna_nome": l.get("coluna_nome"),
        "por_identidade": bool(l.get("por_identidade")) if tem_coluna else False,
    }


def _tipo(l: Linha) -> dict[str, Any]:
    return {
        "nome": l["nome"],
        "categoria": l.get("categoria"),
        "dono": l.get("dono"),
        "comentario": l.get("comentario"),
        "tipo_base": l.get("tipo_base"),
        "obrigatorio": l.get("obrigatorio"),
        "padrao": l.get("padrao"),
        "restricoes": _lista(l.get("restricoes")),
        "valores": _lista(l.get("valores")),
        "atributos": _lista(l.get("atributos")),
    }


def montar_estrutura(schema: str, linhas: Linhas) -> dict[str, Any]:
    """Documento `estrutura/<schema>.json`: nada volátil, tudo ordenado."""
    info = next((l for l in linhas.get("schemas", []) if l["schema"] == schema), {})
    privs = linhas.get("privilegios", [])
    acesso_rel = {
        l["nome"]: _texto(l.get("acesso")) or "ok" for l in privs if l.get("classe") == "relacao"
    }
    acesso_fn = {
        (l["nome"], _texto(l.get("argumentos"))): "ok" if l.get("tem_execute") else "sem_execute"
        for l in privs
        if l.get("classe") == "funcao"
    }
    filhos = {
        k: _por(linhas.get(k, []), "schema", "relacao")
        for k in ("colunas", "restricoes", "indices", "gatilhos")
    }
    views = {l["nome"]: l for l in linhas.get("views", [])}
    deps = _dependencias(linhas.get("dependencias", []))
    relacoes = [
        _relacao(l, filhos, acesso_rel, views, deps) for l in linhas.get("relacoes", [])
    ]
    funcoes = [_funcao(l, acesso_fn) for l in linhas.get("funcoes", [])]
    return {
        "formato": "estrutura",
        "versao_formato": VERSAO_FORMATO,
        "schema": schema,
        "dono": info.get("dono"),
        "comentario": info.get("comentario"),
        "relacoes": _ordenar(relacoes, "nome"),
        "funcoes": _ordenar(funcoes, "nome", "argumentos_identidade"),
        "sequencias": _ordenar([_sequencia(l) for l in linhas.get("sequencias", [])], "nome"),
        "tipos": _ordenar([_tipo(l) for l in linhas.get("tipos", [])], "nome"),
    }


# ----------------------------------------------------------------- estatísticas


def montar_estatisticas(schema: str, linhas: Linhas, extraido_em: str) -> dict[str, Any]:
    """Documento `estatisticas/<schema>.json`: só valores voláteis, junto das relações.

    Linhas de `pg_stats` cujo `tablename` não é uma relação do schema (ex.: índices de
    expressão) são descartadas.
    """
    por_tabela = _por(linhas.get("estatisticas_colunas", []), "tablename")
    relacoes = []
    for l in linhas.get("estatisticas", []):
        colunas = [
            {
                "nome": c["attname"],
                "herdada": bool(c.get("inherited")),
                **{k: json_seguro(c.get(k)) for k in CAMPOS_ESTATISTICA_COLUNA},
            }
            for c in por_tabela.get((l["relacao"],), [])
        ]
        colunas.sort(key=lambda c: (c["nome"], c["herdada"]))
        relacoes.append(
            {
                "nome": l["relacao"],
                "tipo": _mapa(TIPOS_RELACAO, l.get("tipo")),
                **{k: json_seguro(l.get(k)) for k in CAMPOS_ESTATISTICA_RELACAO},
                "colunas": colunas,
            }
        )
    return {
        "formato": "estatisticas",
        "versao_formato": VERSAO_FORMATO,
        "schema": schema,
        "extraido_em": extraido_em,
        "relacoes": _ordenar(relacoes, "nome"),
    }


# ----------------------------------------------------------------- contagens e resumo


def contar_objetos(estrutura: dict[str, Any]) -> dict[str, int]:
    """Contagem por tipo de objeto de um schema (chaves fixas, mesmo quando zero)."""
    relacoes = estrutura["relacoes"]
    contagem = {t: 0 for t in TIPOS_RELACAO.values()}
    for r in relacoes:
        contagem[r["tipo"]] += 1
    contagem.update(
        colunas=sum(len(r["colunas"]) for r in relacoes),
        funcoes=len(estrutura["funcoes"]),
        gatilhos=sum(len(r["gatilhos"]) for r in relacoes),
        indices=sum(len(r["indices"]) for r in relacoes),
        restricoes=sum(len(r["restricoes"]) for r in relacoes),
        sem_select=sum(1 for r in relacoes if r["acesso"] == "sem_select"),
        sequencias=len(estrutura["sequencias"]),
        tipos=len(estrutura["tipos"]),
    )
    return contagem


def somar_contagens(por_schema: dict[str, dict[str, int]]) -> dict[str, int]:
    total: dict[str, int] = {}
    for contagem in por_schema.values():
        for chave, valor in contagem.items():
            total[chave] = total.get(chave, 0) + valor
    return dict(sorted(total.items()))


def montar_banco(
    versao: str, tamanho_bytes: int, extraido_em: str, por_schema: dict[str, dict[str, int]]
) -> dict[str, Any]:
    return {
        "formato": "banco",
        "versao_formato": VERSAO_FORMATO,
        "versao": versao,
        "tamanho_bytes": int(tamanho_bytes),
        "extraido_em": extraido_em,
        "total_schemas": len(por_schema),
        "totais": somar_contagens(por_schema),
        "schemas": dict(sorted(por_schema.items())),
    }


# ----------------------------------------------------------------- validação


def carregar_esquema() -> dict[str, Any]:
    texto = (resources.files("pg_atlas.esquemas") / "catalogo.schema.json").read_text(
        encoding="utf-8"
    )
    return json.loads(texto)


def validar(documento: dict[str, Any], rotulo: str = "documento") -> None:
    """Valida contra `catalogo.schema.json`; falha com mensagem clara apontando o caminho."""
    esquema = carregar_esquema()
    formato = documento.get("formato") if isinstance(documento, dict) else None
    if formato not in FORMATOS:
        raise ErroValidacaoCatalogo(
            f"{rotulo}: campo 'formato' ausente ou inválido (esperado um de {', '.join(FORMATOS)})."
        )
    # Valida pelo definição do formato (mensagens mais claras que as do oneOf da raiz).
    parcial = {"$schema": esquema["$schema"], "$ref": f"#/$defs/{formato}", "$defs": esquema["$defs"]}
    validador = jsonschema.Draft202012Validator(parcial)
    erros = sorted(validador.iter_errors(documento), key=lambda e: [str(p) for p in e.path])
    if not erros:
        return
    primeiro = erros[0]
    onde = "/".join(str(p) for p in primeiro.absolute_path) or "(raiz)"
    raise ErroValidacaoCatalogo(
        f"{rotulo} fora do formato do catálogo em {onde}: {primeiro.message[:200]} "
        f"({len(erros)} problema(s) no total)."
    )
