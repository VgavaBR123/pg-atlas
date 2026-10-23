"""Nomes de arquivo das fichas e mapa objeto -> caminho (decisão D-013, item 5).

Ordem de aplicação: marcador de mascaramento (D-009), minúsculas, caracteres permitidos,
nome reservado do Windows, nome longo, grupo de colisão (`__<n>`), conferência final.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass

from pg_atlas.erros import ErroAtlas
from pg_atlas.mascara import nome_de_arquivo

RESERVADOS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(10)}
    | {f"lpt{i}" for i in range(10)}
)
PERMITIDOS_ESPECIAIS = "_-$"
LIMITE_NOME = 120
CORTE_NOME = 100
PASTA_RELACOES = "tabelas"
PASTA_ROTINAS = "funcoes"


class ErroEsqueleto(ErroAtlas):
    """Entrada ausente, colisão de nomes ou saída que não pode ser gerada."""

    codigo_saida = 4


@dataclass(frozen=True, order=True)
class Objeto:
    """Objeto que recebe ficha. `args` só existe para rotinas (argumentos de identidade)."""

    schema: str
    pasta: str
    tipo: str
    nome: str
    args: str | None = None


def nome_base(nome: str) -> str:
    """Nome de arquivo sem extensão e sem sufixo de colisão."""
    minusculo = nome_de_arquivo(nome).lower()
    saida: list[str] = []
    for caractere in minusculo:
        if caractere.isalnum() or caractere in PERMITIDOS_ESPECIAIS:
            saida.append(caractere)
        else:
            saida.extend(f"~{byte:02X}" for byte in caractere.encode("utf-8"))
    base = "".join(saida)
    if base in RESERVADOS:
        base += "~"
    if len(base) > LIMITE_NOME:
        base = base[:CORTE_NOME] + "~" + hashlib.sha1(nome.encode("utf-8")).hexdigest()[:8]
    return base


def pasta_do_schema(schema: str) -> str:
    return nome_base(schema)


def caminho_readme(schema: str) -> str:
    return f"schemas/{pasta_do_schema(schema)}/README.md"


def atribuir_caminhos(objetos: Iterable[Objeto]) -> dict[Objeto, str]:
    """Caminho (relativo à raiz do repositório de documentação, com `/`) de cada objeto."""
    grupos: dict[tuple[str, str, str], list[Objeto]] = {}
    for objeto in objetos:
        grupos.setdefault((objeto.schema, objeto.pasta, nome_base(objeto.nome)), []).append(objeto)
    caminhos: dict[Objeto, str] = {}
    for (schema, pasta, base), membros in sorted(grupos.items()):
        ordenados = sorted(membros, key=lambda o: (o.nome, o.args or ""))
        for n, objeto in enumerate(ordenados, start=1):
            arquivo = base if len(ordenados) == 1 else f"{base}__{n}"
            caminhos[objeto] = f"schemas/{pasta_do_schema(schema)}/{pasta}/{arquivo}.md"
    conferir_unicidade(caminhos, {o.schema for o in caminhos})
    return caminhos


def conferir_unicidade(caminhos: dict[Objeto, str], schemas: Iterable[str]) -> None:
    """Falha, sem gerar nada, se sobrar colisão (sem diferenciar caixa)."""
    pastas: dict[str, str] = {}
    for schema in sorted(set(schemas)):
        chave = pasta_do_schema(schema).casefold()
        if chave in pastas:
            raise ErroEsqueleto(
                f"Colisão de pasta: os schemas '{pastas[chave]}' e '{schema}' dão a mesma pasta."
            )
        pastas[chave] = schema
    vistos: dict[str, Objeto] = {}
    for objeto, caminho in sorted(caminhos.items()):
        chave = caminho.casefold()
        if chave in vistos:
            raise ErroEsqueleto(
                f"Colisão de nome de arquivo ({caminho}) entre '{vistos[chave].nome}' "
                f"e '{objeto.nome}'. Nada foi gerado."
            )
        vistos[chave] = objeto


def montar_mapa(caminhos: dict[Objeto, str]) -> list[dict[str, str | None]]:
    """Itens de `catalogo/_fichas.json`, ordenados."""
    itens = [
        {
            "schema": o.schema,
            "tipo": o.tipo,
            "nome": o.nome,
            "argumentos_identidade": o.args,
            "caminho": caminho,
        }
        for o, caminho in caminhos.items()
    ]
    return sorted(
        itens,
        key=lambda i: (i["schema"], i["tipo"], i["nome"], i["argumentos_identidade"] or "", i["caminho"]),
    )
