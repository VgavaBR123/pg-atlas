"""Checagens offline da verificação do catálogo (tarefa 1.5): lêem só arquivos de `<doc>`.

Cada checagem devolve um `Verificacao`. Nenhuma mensagem carrega valor de dado: só nomes de
arquivo, caminhos de campo (com nomes mascarados), nomes de schema e contagens.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import jsonschema

from pg_atlas import catalogo_montagem as montagem
from pg_atlas import mascara, triagem
from pg_atlas.erros import ErroAtlas
from pg_atlas.esqueleto import MAPA, ORFAS
from pg_atlas.esqueleto_contexto import construir
from pg_atlas.esqueleto_nomes import caminho_readme, montar_mapa
from pg_atlas.guarda import COLUNAS_ESTATISTICA_PROIBIDAS

LIMITE_PROBLEMAS = 50
# Chaves de JSON proibidas: as colunas do guarda e semelhantes (valores reais de amostra).
PADRAO_CHAVE_PROIBIDA = re.compile(
    r"most_common|histogram|elem_count|stavalues|stanumbers", re.IGNORECASE
)
VALORES_PROIBIDOS = frozenset(COLUNAS_ESTATISTICA_PROIBIDAS)
PASTAS_DE_FICHA = ("tabelas", "funcoes")


@dataclass
class Verificacao:
    """Resultado de uma verificação: OK se não houver problemas."""

    nome: str
    descricao: str
    problemas: list[str] = field(default_factory=list)
    resumo: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problemas

    def como_json(self) -> dict[str, Any]:
        return {
            "nome": self.nome,
            "descricao": self.descricao,
            "ok": self.ok,
            "resumo": dict(sorted(self.resumo.items())),
            "total_problemas": len(self.problemas),
            "problemas": self.problemas[:LIMITE_PROBLEMAS],
        }


def ler_json(caminho: Path) -> Any:
    return json.loads(caminho.read_text(encoding="utf-8"))


def contagens_do_catalogo(raiz: Path, schemas: list[str]) -> dict[str, dict[str, int]]:
    """Contagem por schema recalculada a partir de `estrutura/<schema>.json` (os que existem)."""
    saida: dict[str, dict[str, int]] = {}
    for schema in schemas:
        arquivo = raiz / "estrutura" / f"{schema}.json"
        if arquivo.is_file():
            saida[schema] = montagem.contar_objetos(ler_json(arquivo))
    return saida


# ----------------------------------------------------------------- cobertura


def schemas_do_banco_json(raiz: Path) -> list[str]:
    arquivo = raiz / "_banco.json"
    if not arquivo.is_file():
        return []
    try:
        bruto = ler_json(arquivo).get("schemas")
    except (json.JSONDecodeError, AttributeError):
        return []
    return sorted(bruto) if isinstance(bruto, dict) else []


def verificar_cobertura(doc: Path) -> Verificacao:
    """Todo schema do `_banco.json` tem estrutura e estatísticas; `estado.json` da 1.2 fecha."""
    v = Verificacao("cobertura", "Todo schema do _banco.json tem estrutura, estatísticas e status concluído")
    raiz = doc / "catalogo"
    schemas = schemas_do_banco_json(raiz)
    if not schemas:
        v.problemas.append("_banco.json ausente, ilegível ou sem schemas")
        return v
    for pasta in ("estrutura", "estatisticas"):
        for s in schemas:
            if not (raiz / pasta / f"{s}.json").is_file():
                v.problemas.append(f"schema '{s}' sem arquivo {pasta}/{s}.json")
        extras = sorted(p.stem for p in (raiz / pasta).glob("*.json") if p.stem not in schemas)
        v.problemas += [f"{pasta}/{s}.json sem schema no _banco.json" for s in extras]
    v.resumo["schemas"] = len(schemas)
    _conferir_estado(doc / "_controle" / "estado.json", schemas, v)
    return v


def _conferir_estado(caminho: Path, schemas: list[str], v: Verificacao) -> None:
    try:
        tarefa = ler_json(caminho)["fases"]["1"]["tarefas"]["1.2"]
        mapa = tarefa.get("schemas") or {}
        progresso = tarefa["progresso"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        v.problemas.append("estado.json sem a tarefa 1.2 com progresso")
        return
    v.resumo["estado_concluidos"] = int(progresso.get("concluidos", -1))
    v.resumo["estado_total"] = int(progresso.get("total", -1))
    if progresso.get("concluidos") != len(schemas) or progresso.get("total") != len(schemas):
        v.problemas.append(
            f"estado.json da 1.2 mostra {progresso.get('concluidos')}/{progresso.get('total')}, "
            f"esperado {len(schemas)}/{len(schemas)}"
        )
    pendentes = sorted(s for s in schemas if mapa.get(s) != "concluido")
    v.problemas += [f"estado.json: schema '{s}' não está concluído" for s in pendentes]


# ----------------------------------------------------------------- esquemas e proibidos


def _carregar_esquema(nome: str) -> dict[str, Any]:
    return json.loads((resources.files("pg_atlas.esquemas") / nome).read_text(encoding="utf-8"))


def _validar_fichas(documento: Any) -> None:
    erros = sorted(
        jsonschema.Draft202012Validator(_carregar_esquema("fichas.schema.json")).iter_errors(documento),
        key=lambda e: [str(p) for p in e.path],
    )
    if erros:
        onde = "/".join(str(p) for p in erros[0].absolute_path) or "(raiz)"
        raise ErroAtlas(f"fora do formato em {onde}: {erros[0].message[:120]} ({len(erros)} problema(s))")


def _validar_arquivo(relativo: str, documento: Any) -> None:
    """Valida o documento pelo formato esperado do caminho; levanta ErroAtlas se inválido."""
    pasta, _, nome = relativo.rpartition("/")
    esperado = {"estrutura": "estrutura", "estatisticas": "estatisticas"}.get(pasta.split("/")[-1])
    if nome == "_banco.json":
        esperado = "banco"
    elif nome == "mascaramento.json":
        esperado = "mascaramento"
    if nome == "_triagem.json":
        triagem.validar(documento)
    elif nome == MAPA:
        _validar_fichas(documento)
    elif esperado:
        if not isinstance(documento, dict) or documento.get("formato") != esperado:
            raise ErroAtlas(f"campo 'formato' diferente de '{esperado}'")
        montagem.validar(documento, rotulo=nome)
    else:
        raise ErroAtlas("arquivo sem esquema conhecido")


def arquivos_do_catalogo(doc: Path) -> Iterator[tuple[str, Path]]:
    """(caminho relativo, arquivo) de todo JSON verificado, em ordem."""
    achados = [(p.relative_to(doc).as_posix(), p) for p in (doc / "catalogo").rglob("*.json")]
    mascaramento = doc / "_controle" / "mascaramento.json"
    if mascaramento.is_file():
        achados.append((mascaramento.relative_to(doc).as_posix(), mascaramento))
    yield from sorted(achados)


def achar_proibidos(documento: Any, caminho: str = "") -> Iterator[str]:
    """Caminhos de chaves proibidas e de valores que são exatamente um nome proibido."""
    if isinstance(documento, dict):
        for chave, valor in documento.items():
            if PADRAO_CHAVE_PROIBIDA.search(str(chave)):
                yield f"{caminho}/chave proibida '{chave}'"
            yield from achar_proibidos(valor, f"{caminho}/{chave}")
    elif isinstance(documento, list):
        for i, item in enumerate(documento):
            yield from achar_proibidos(item, f"{caminho}[{i}]")
    elif isinstance(documento, str) and documento.strip().lower() in VALORES_PROIBIDOS:
        yield f"{caminho}: valor proibido '{documento.strip().lower()}'"


def verificar_arquivos(doc: Path) -> tuple[Verificacao, Verificacao]:
    """Uma passada por todos os JSON: (validação de esquema, varredura de proibidos)."""
    esq = Verificacao("esquemas", "Todo JSON do catálogo é válido contra o JSON Schema")
    pro = Verificacao("proibidos", "Nenhuma chave/valor proibido nem CPF/CNPJ sem máscara no catálogo")
    total = 0
    for relativo, arquivo in arquivos_do_catalogo(doc):
        total += 1
        try:
            documento = ler_json(arquivo)
        except (OSError, json.JSONDecodeError):
            esq.problemas.append(f"{relativo}: JSON ilegível")
            continue
        try:
            _validar_arquivo(relativo, documento)
        except ErroAtlas as erro:
            esq.problemas.append(f"{relativo}: {erro}")
        pro.problemas += [f"{relativo}{a}" for a in achar_proibidos(documento)]
        for onde, contagem in mascara.varrer_documento(documento):
            for tipo, n in sorted(contagem.items()):
                pro.problemas.append(f"{relativo}: {onde}: {tipo} sem máscara x{n}")
    esq.resumo["arquivos"] = pro.resumo["arquivos"] = total
    if total == 0:
        esq.problemas.append("nenhum JSON encontrado em catalogo/")
    return esq, pro


# ----------------------------------------------------------------- fichas


def _chave_ficha(item: dict[str, Any]) -> str:
    return json.dumps(item, sort_keys=True, ensure_ascii=False)


def _fichas_existentes(doc: Path) -> set[str]:
    raiz = doc / "schemas"
    if not raiz.is_dir():
        return set()
    return {
        p.relative_to(doc).as_posix()
        for pasta in PASTAS_DE_FICHA
        for p in raiz.glob(f"*/{pasta}/*.md")
    }


def verificar_fichas(doc: Path, nome: str = "", descricao: str = "") -> Verificacao:
    """Todo objeto do catálogo tem ficha no esqueleto e vice-versa (D-013)."""
    v = Verificacao("fichas", "Todo objeto do catálogo tem ficha no esqueleto e vice-versa")
    try:
        ctx = construir(doc, nome, descricao)
    except ErroAtlas as erro:
        v.problemas.append(f"não foi possível calcular as fichas esperadas: {erro}")
        return v
    esperado = montar_mapa(ctx.caminhos)
    caminhos = {i["caminho"] for i in esperado}
    v.resumo.update(esperadas=len(esperado), readmes=len(ctx.schemas))
    _conferir_mapa(doc, esperado, v)
    existentes = _fichas_existentes(doc)
    v.resumo["arquivos_existentes"] = len(existentes)
    for c in sorted(caminhos - existentes):
        v.problemas.append(f"ficha faltando: {c}")
    for c in sorted(existentes - caminhos):
        v.problemas.append(f"ficha sem objeto no catálogo (órfã): {c}")
    for s in ctx.schemas:
        if not (doc / caminho_readme(s)).is_file():
            v.problemas.append(f"README faltando: {caminho_readme(s)}")
    _conferir_orfas(doc / "_controle" / ORFAS, v)
    return v


def _conferir_mapa(doc: Path, esperado: list[dict[str, Any]], v: Verificacao) -> None:
    arquivo = doc / "catalogo" / MAPA
    try:
        atual = ler_json(arquivo)["fichas"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        v.problemas.append(f"catalogo/{MAPA} ausente ou ilegível")
        return
    v.resumo["no_mapa"] = len(atual)
    chaves_a = {_chave_ficha(i) for i in atual if isinstance(i, dict)}
    chaves_e = {_chave_ficha(i) for i in esperado}
    v.problemas += [
        f"objeto sem ficha no mapa: {json.loads(c)['caminho']}" for c in sorted(chaves_e - chaves_a)
    ]
    v.problemas += [
        f"ficha no mapa sem objeto no catálogo: {json.loads(c).get('caminho')}"
        for c in sorted(chaves_a - chaves_e)
    ]


def _conferir_orfas(arquivo: Path, v: Verificacao) -> None:
    try:
        orfas = ler_json(arquivo)["orfas"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        v.problemas.append(f"_controle/{ORFAS} ausente ou ilegível")
        return
    v.resumo["orfas_registradas"] = len(orfas)
    v.problemas += [f"_controle/{ORFAS} lista órfã: {o}" for o in orfas]
