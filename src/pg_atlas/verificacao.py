"""Comando `verificar catalogo`: confere o catálogo da fase 1 (tarefa 1.5 do plano).

Verificações (cada uma imprime OK ou FALHA):
- `contagem`: compara, por schema, o número de objetos do catálogo com uma contagem direta
  no banco (`sql/verificacao/contagens.sql`, só catálogo) e acusa schema do escopo sem arquivo;
- `esquemas`: valida todo JSON do catálogo contra o JSON Schema;
- `proibidos`: procura chaves/valores proibidos e CPF/CNPJ sem máscara;
- `fichas`: todo objeto tem ficha no esqueleto e vice-versa;
- `cobertura`: todo schema do `_banco.json` tem estrutura e estatísticas, e o `estado.json` fecha.

`--sem-banco` pula só a `contagem` (teste offline). Grava `_controle/verificacao-catalogo.json`
(determinístico; o horário fica no campo `verificado_em`). Sai com 0 se tudo passa, 1 se algo falha.
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any

from pg_atlas import catalogo
from pg_atlas import verificacao_checagens as checagens
from pg_atlas.cli import adicionar_argumentos_comuns
from pg_atlas.conexao import Conexao
from pg_atlas.config import Projeto, carregar_projeto
from pg_atlas.json_util import escrever_json
from pg_atlas.verificacao_checagens import Verificacao

PACOTE_SQL = "pg_atlas.sql.verificacao"
SAIDA = "verificacao-catalogo.json"
VERSAO_FORMATO = 1
CAMPOS_CONTAGEM = (
    "tabela", "particionada", "view", "materializada", "estrangeira", "sequencias", "funcoes",
)  # fmt: skip


def carregar_sql(nome: str) -> str:
    return (resources.files(PACOTE_SQL) / f"{nome}.sql").read_text(encoding="utf-8")


def contar_no_banco(fonte: catalogo.FonteSql) -> dict[str, dict[str, int]]:
    """Contagem por schema direto no banco (uma consulta leve de catálogo)."""
    colunas, linhas = fonte.executar_com_colunas(carregar_sql("contagens"))
    saida: dict[str, dict[str, int]] = {}
    for linha in linhas:
        registro = dict(zip(colunas, linha))
        saida[str(registro["schema"])] = {c: int(registro[c]) for c in CAMPOS_CONTAGEM}
    return saida


def comparar_contagens(
    no_banco: dict[str, dict[str, int]],
    no_catalogo: dict[str, dict[str, int]],
    escopo: list[str],
    schemas_catalogo: list[str],
) -> Verificacao:
    """Compara número de objetos por schema; acusa schema do escopo sem arquivo e vice-versa."""
    v = Verificacao("contagem", "Contagem de objetos por schema no catálogo igual à do banco")
    for s in sorted(set(escopo) - set(schemas_catalogo)):
        v.problemas.append(f"schema '{s}' existe no banco (escopo) e não tem arquivo no catálogo")
    for s in sorted(set(schemas_catalogo) - set(no_banco)):
        v.problemas.append(f"schema '{s}' está no catálogo e não existe no banco")
    for s in sorted(set(schemas_catalogo) & set(no_banco)):
        if s not in no_catalogo:
            v.problemas.append(f"schema '{s}' sem estrutura/{s}.json para comparar")
            continue
        for campo in CAMPOS_CONTAGEM:
            if no_catalogo[s][campo] != no_banco[s][campo]:
                v.problemas.append(
                    f"schema '{s}', {campo}: catálogo {no_catalogo[s][campo]}, banco {no_banco[s][campo]}"
                )
    v.resumo["schemas_comparados"] = len(set(schemas_catalogo) & set(no_banco))
    v.resumo["objetos_catalogo"] = sum(
        sum(c[k] for k in CAMPOS_CONTAGEM) for s, c in no_catalogo.items() if s in no_banco
    )
    v.resumo["objetos_banco"] = sum(
        sum(c[k] for k in CAMPOS_CONTAGEM) for s, c in no_banco.items() if s in schemas_catalogo
    )
    return v


def verificar_contagem(projeto: Projeto, fonte: catalogo.FonteSql) -> Verificacao:
    raiz = projeto.doc / "catalogo"
    schemas = checagens.schemas_do_banco_json(raiz)
    return comparar_contagens(
        contar_no_banco(fonte),
        checagens.contagens_do_catalogo(raiz, schemas),
        catalogo.listar_schemas(fonte, projeto),
        schemas,
    )


def verificar_catalogo(
    projeto: Projeto, fonte: catalogo.FonteSql | None
) -> list[Verificacao]:
    """Roda as verificações, na ordem do plano. Sem `fonte`, a contagem no banco é pulada."""
    resultados: list[Verificacao] = []
    if fonte is not None:
        resultados.append(verificar_contagem(projeto, fonte))
    esquemas, proibidos = checagens.verificar_arquivos(projeto.doc)
    resultados += [esquemas, proibidos]
    resultados.append(checagens.verificar_fichas(projeto.doc, projeto.nome, projeto.descricao))
    resultados.append(checagens.verificar_cobertura(projeto.doc))
    return resultados


def montar_relatorio(
    resultados: list[Verificacao], banco_consultado: bool, verificado_em: str
) -> dict[str, Any]:
    return {
        "formato": "verificacao-catalogo",
        "versao_formato": VERSAO_FORMATO,
        "ok": all(r.ok for r in resultados),
        "banco_consultado": banco_consultado,
        "verificacoes": [r.como_json() for r in resultados],
        "verificado_em": verificado_em,
    }


def imprimir_relatorio(
    resultados: list[Verificacao], imprimir: Callable[[str], None] = print
) -> None:
    for r in resultados:
        resumo = ", ".join(f"{k}={v}" for k, v in sorted(r.resumo.items()))
        imprimir(f"[{'OK' if r.ok else 'FALHA'}] {r.nome}: {r.descricao}" + (f" ({resumo})" if resumo else ""))
        for problema in r.problemas[: checagens.LIMITE_PROBLEMAS]:
            imprimir(f"    - {problema}")
        if len(r.problemas) > checagens.LIMITE_PROBLEMAS:
            imprimir(f"    ... e mais {len(r.problemas) - checagens.LIMITE_PROBLEMAS} problema(s)")
    falhas = [r.nome for r in resultados if not r.ok]
    if falhas:
        imprimir(f"Resumo: {len(falhas)} de {len(resultados)} verificação(ões) falharam ({', '.join(falhas)}).")
    else:
        imprimir(f"Resumo: {len(resultados)} verificações OK.")


def agora_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def executar_catalogo(projeto: Projeto, fonte: catalogo.FonteSql | None) -> int:
    resultados = verificar_catalogo(projeto, fonte)
    imprimir_relatorio(resultados)
    escrever_json(
        projeto.controle / SAIDA, montar_relatorio(resultados, fonte is not None, agora_utc())
    )
    return 0 if all(r.ok for r in resultados) else 1


def registrar(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("verificar", help="verificações de fase (catálogo)")
    alvos = parser.add_subparsers(dest="alvo", metavar="alvo", required=True)
    catalogo_parser = alvos.add_parser(
        "catalogo", help="confere o catálogo da fase 1 contra o banco, os esquemas e as fichas"
    )
    adicionar_argumentos_comuns(catalogo_parser)
    catalogo_parser.add_argument(
        "--sem-banco", action="store_true", help="não conecta: pula só a contagem no banco"
    )
    catalogo_parser.set_defaults(func=executar)


def executar(args: argparse.Namespace) -> int:
    projeto = carregar_projeto(args.doc, args.workspace)
    inicio = time.monotonic()
    if args.sem_banco:
        codigo = executar_catalogo(projeto, None)
    else:
        with Conexao(projeto) as conexao:
            codigo = executar_catalogo(projeto, conexao)
    print(f"Verificação concluída em {time.monotonic() - inicio:.1f}s (código {codigo}).")
    return codigo
