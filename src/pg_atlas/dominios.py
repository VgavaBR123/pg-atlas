"""Comando `dominios ler`: valores das tabelas de domínio aprovadas (decisão D-023, item 8).

Única leitura de conteúdo de tabela do projeto. Regras:

- só as tabelas listadas em `config/dominios.yml` (lista `aprovadas`), pela `Conexao` e pelo guarda de
  SQL no modo de leitura de valores (o guarda recusa qualquer outra tabela);
- uma consulta por tabela, `LIMIT 500`, `ORDER BY` pela chave primária (ou por todas as colunas lidas);
- colunas `bytea` e de tipos grandes (json, xml, tsvector...) são omitidas; texto é cortado a 200 caracteres;
- todo valor passa pelo detector de `mascara.py` (CPF, CNPJ, e-mail, telefone, IP, credencial). Valor
  detectado é mascarado e a tabela entra em `duvidas.md` para revisão (só nome da tabela e das colunas);
- saída: `catalogo/dominios/<schema>.<tabela>.json` e `catalogo/dominios/indice.json`, determinísticos;
- retomável (tabela já `lida`, `truncada` ou `sem_privilegio` é pulada; `erro` é tentada de novo),
  com pausa entre consultas e tolerante a erro por tabela.

Nada aqui é específico de um banco: as tabelas e o catálogo vêm da configuração e dos arquivos do projeto.
"""

from __future__ import annotations

import argparse
import datetime
import decimal
import json
import math
import re
import time
from collections import Counter
from collections.abc import Callable, Collection
from importlib import resources
from pathlib import Path
from typing import Any, Protocol

import jsonschema
import yaml

from pg_atlas.cli import adicionar_argumentos_comuns
from pg_atlas.config import carregar_projeto
from pg_atlas.erros import ErroAtlas, ErroConexao, ErroExecucao, SqlRejeitado
from pg_atlas.json_util import escrever_json
from pg_atlas.mascara import Mascarador, sem_numero

PASTA_SAIDA = ("catalogo", "dominios")
ARQ_INDICE = "indice.json"
VERSAO_FORMATO = 1
LIMITE_LINHAS = 500
MAX_TEXTO = 200
PAUSA_PADRAO_S = 0.5
SITUACOES_FINAIS = ("lida", "truncada", "sem_privilegio")  # não são refeitas na retomada
SITUACOES = (*SITUACOES_FINAIS, "erro")
TIPOS_OMITIDOS = frozenset({"bytea", "json", "jsonb", "xml", "tsvector", "tsquery", "bit", "varbit"})
TIPOS_TEXTO = frozenset({"text", "character", "varchar", "char", "bpchar", "citext", "name"})
MARCA_INICIO = "<!-- dominios:inicio -->"
MARCA_FIM = "<!-- dominios:fim -->"
SQLSTATE_SEM_PRIVILEGIO = "42501"
MAX_MOTIVO = 300


class ErroDominios(ErroAtlas):
    """Configuração ou arquivo de domínio inválido."""

    codigo_saida = 4


class Executor(Protocol):
    """O que a leitura precisa da `Conexao` (facilita testes sem banco)."""

    def executar_com_colunas(
        self, sql: str, params: Any = None, *, tabelas_liberadas: Collection[str] | None = None
    ) -> tuple[list[str], list[tuple[Any, ...]]]: ...


# ----------------------------------------------------------------- configuração e arquivos


def ler_dominios_aprovados(doc: Path) -> set[str]:
    """Nomes `schema.tabela` de `config/dominios.yml` (lista `aprovadas`). Arquivo ausente: conjunto vazio."""
    caminho = doc / "config" / "dominios.yml"
    if not caminho.is_file():
        return set()
    try:
        dados = yaml.safe_load(caminho.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        raise ErroDominios(f"YAML inválido em {caminho}") from None
    itens = dados.get("aprovadas") if isinstance(dados, dict) else None
    return {str(i) for i in itens or [] if isinstance(i, str)}


def pasta_saida(doc: Path) -> Path:
    return doc.joinpath(*PASTA_SAIDA)


def nome_arquivo(tabela: str) -> str:
    return f"{tabela}.json"


def _esquema(nome: str) -> dict[str, Any]:
    return json.loads((resources.files("pg_atlas.esquemas") / nome).read_text(encoding="utf-8"))


def validar(documento: Any, esquema: str, rotulo: str) -> None:
    erros = sorted(
        jsonschema.Draft202012Validator(_esquema(esquema)).iter_errors(documento),
        key=lambda e: [str(p) for p in e.path],
    )
    if erros:
        onde = "/".join(str(p) for p in erros[0].absolute_path) or "(raiz)"
        raise ErroDominios(f"{rotulo} fora do formato em {onde}: {erros[0].message[:200]} ({len(erros)} problema(s)).")


def ler_resultado(caminho: Path) -> dict[str, Any] | None:
    """Arquivo de uma tabela já gravado e válido; `None` se não existir ou estiver ilegível/inválido."""
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
        validar(dados, "dominio-tabela.schema.json", caminho.name)
    except (OSError, json.JSONDecodeError, ErroDominios):
        return None
    return dados  # type: ignore[no-any-return]


def ler_resultados(doc: Path, aprovadas: Collection[str]) -> dict[str, dict[str, Any]]:
    """Resultados gravados, só das tabelas aprovadas (a tabela que saiu do YAML é ignorada)."""
    pasta = pasta_saida(doc)
    saida: dict[str, dict[str, Any]] = {}
    for tabela in sorted(aprovadas):
        dados = ler_resultado(pasta / nome_arquivo(tabela))
        if dados is not None and dados["tabela"] == tabela:
            saida[tabela] = dados
    return saida


# ----------------------------------------------------------------- consulta


def _ident(nome: str) -> str:
    return '"' + nome.replace('"', '""') + '"'


def _tipo_base(tipo: str | None) -> str:
    return re.split(r"[\s(\[]", (tipo or "").strip().lower(), maxsplit=1)[0]


def _omitida(tipo: str | None) -> bool:
    return _tipo_base(tipo) in TIPOS_OMITIDOS


def _texto(tipo: str | None) -> bool:
    return _tipo_base(tipo) in TIPOS_TEXTO and not (tipo or "").strip().endswith("]")


def separar_colunas(relacao: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(lidas, omitidas) na ordem das colunas do catálogo; omitidas levam só nome e tipo."""
    colunas = sorted(relacao.get("colunas", []), key=lambda c: (c.get("ordem") or 0, c["nome"]))
    lidas = [c for c in colunas if not _omitida(c.get("tipo"))]
    omitidas = [{"nome": c["nome"], "tipo": c.get("tipo")} for c in colunas if _omitida(c.get("tipo"))]
    return lidas, omitidas


def ordem_da_consulta(relacao: dict[str, Any], lidas: list[dict[str, Any]]) -> list[str]:
    """Colunas do `ORDER BY`: a chave primária (se todas legíveis) ou todas as colunas lidas."""
    nomes = [c["nome"] for c in lidas]
    for r in sorted(relacao.get("restricoes", []), key=lambda r: r["nome"]):
        if r.get("tipo") == "pk" and r.get("colunas") and all(c in nomes for c in r["colunas"]):
            return list(r["colunas"])
    return nomes


def montar_sql(schema: str, nome: str, lidas: list[dict[str, Any]], ordem: list[str], limite: int) -> str:
    """SELECT com LIMIT; texto é cortado no servidor (`left`) para não trafegar valores enormes."""
    itens = [
        f"left({_ident(c['nome'])}, {MAX_TEXTO + 1}) AS {_ident(c['nome'])}" if _texto(c.get("tipo")) else _ident(c["nome"])
        for c in lidas
    ]
    return (
        f"SELECT {', '.join(itens)} FROM {_ident(schema)}.{_ident(nome)} "
        f"ORDER BY {', '.join(_ident(c) for c in ordem)} LIMIT {int(limite)}"
    )


# ----------------------------------------------------------------- valores e máscara


def _json_simples(valor: Any) -> Any:
    """Valor do banco em tipo JSON: bool, int, float finito, texto ou None."""
    if valor is None or isinstance(valor, (bool, int, str)):
        return valor
    if isinstance(valor, float):
        return valor if math.isfinite(valor) else str(valor)
    if isinstance(valor, decimal.Decimal):
        if not valor.is_finite():
            return str(valor)
        return int(valor) if valor == valor.to_integral_value() else float(valor)
    if isinstance(valor, (datetime.datetime, datetime.date, datetime.time)):
        return valor.isoformat()
    if isinstance(valor, (list, tuple, dict, set, frozenset)):
        lista = sorted(valor, key=str) if isinstance(valor, (set, frozenset)) else valor
        return json.dumps(lista, default=str, ensure_ascii=False, sort_keys=True)
    return str(valor)


def _cortar_texto(texto: str) -> str:
    return texto if len(texto) <= MAX_TEXTO else texto[:MAX_TEXTO] + "…"


def tratar_valor(valor: Any, coluna: str, mascarador: Mascarador, achados: Counter[tuple[str, str]]) -> Any:
    """Converte, corta e passa pelo detector; valor com dado pessoal ou credencial vira marcador."""
    simples = _json_simples(valor)
    if simples is None or isinstance(simples, bool):
        return simples
    texto = _cortar_texto(simples) if isinstance(simples, str) else str(simples)
    mascarado, contagem = mascarador.mascarar(texto)
    if not contagem:
        return texto if isinstance(simples, str) else simples
    for tipo, n in contagem.items():
        achados[(coluna, tipo)] += n
    return sem_numero(mascarado)


def tratar_linhas(
    colunas: list[str], linhas: list[tuple[Any, ...]]
) -> tuple[list[list[Any]], list[dict[str, Any]]]:
    mascarador = Mascarador()
    achados: Counter[tuple[str, str]] = Counter()
    saida = [[tratar_valor(v, colunas[i], mascarador, achados) for i, v in enumerate(linha)] for linha in linhas]
    mascaramentos = [{"coluna": c, "tipo": t, "quantidade": n} for (c, t), n in sorted(achados.items())]
    return saida, mascaramentos


# ----------------------------------------------------------------- uma tabela


def _resultado(
    tabela: str, situacao: str, *, motivo: str | None = None, colunas: list[str] | None = None,
    omitidas: list[dict[str, Any]] | None = None, ordem: list[str] | None = None,
    linhas: list[list[Any]] | None = None, mascaramentos: list[dict[str, Any]] | None = None,
    limite: int = LIMITE_LINHAS,
) -> dict[str, Any]:
    schema, nome = tabela.split(".", 1)
    return {
        "formato": "dominio_tabela",
        "versao_formato": VERSAO_FORMATO,
        "tabela": tabela,
        "schema": schema,
        "nome": nome,
        "situacao": situacao,
        "motivo": motivo,
        "limite": limite,
        "total_linhas": len(linhas or []),
        "colunas": colunas or [],
        "colunas_omitidas": omitidas or [],
        "ordenado_por": ordem or [],
        "linhas": linhas or [],
        "mascaramentos": mascaramentos or [],
        "revisar": bool(mascaramentos),
    }


def ler_tabela(
    tabela: str, relacao: dict[str, Any] | None, conexao: Executor, aprovadas: Collection[str], limite: int = LIMITE_LINHAS
) -> dict[str, Any]:
    """Lê uma tabela e devolve o resultado (nunca lança por erro de tabela, só por perda de conexão)."""
    schema, nome = tabela.split(".", 1)
    if relacao is None:
        return _resultado(tabela, "erro", motivo="tabela fora do catálogo", limite=limite)
    if relacao.get("acesso") == "sem_select":
        return _resultado(tabela, "sem_privilegio", motivo="sem SELECT segundo o catálogo", limite=limite)
    lidas, omitidas = separar_colunas(relacao)
    if not lidas:
        return _resultado(tabela, "erro", motivo="nenhuma coluna legível (todas omitidas)", omitidas=omitidas, limite=limite)
    ordem = ordem_da_consulta(relacao, lidas)
    sql = montar_sql(schema, nome, lidas, ordem, limite)
    try:
        nomes, linhas = conexao.executar_com_colunas(sql, tabelas_liberadas=sorted(aprovadas))
    except ErroExecucao as erro:
        if erro.sqlstate == SQLSTATE_SEM_PRIVILEGIO:
            return _resultado(tabela, "sem_privilegio", motivo="sem privilégio de SELECT", omitidas=omitidas, limite=limite)
        return _resultado(tabela, "erro", motivo=str(erro)[:MAX_MOTIVO], omitidas=omitidas, limite=limite)
    except SqlRejeitado as erro:
        return _resultado(tabela, "erro", motivo=str(erro)[:MAX_MOTIVO], omitidas=omitidas, limite=limite)
    valores, mascaramentos = tratar_linhas(nomes, linhas)
    situacao = "truncada" if len(valores) >= limite else "lida"
    motivo = f"atingiu o limite de {limite} linhas" if situacao == "truncada" else None
    return _resultado(
        tabela, situacao, motivo=motivo, colunas=nomes, omitidas=omitidas, ordem=ordem,
        linhas=valores, mascaramentos=mascaramentos, limite=limite,
    )  # fmt: skip


# ----------------------------------------------------------------- índice e dúvidas


def montar_indice(aprovadas: Collection[str], resultados: dict[str, dict[str, Any]], limite: int = LIMITE_LINHAS) -> dict[str, Any]:
    tabelas = [
        {
            "tabela": t,
            "arquivo": nome_arquivo(t),
            "situacao": r["situacao"],
            "linhas": r["total_linhas"],
            "revisar": r["revisar"],
            "motivo": r["motivo"],
        }
        for t, r in sorted(resultados.items())
    ]
    contagem = Counter(r["situacao"] for r in resultados.values())
    return {
        "formato": "dominio_indice",
        "versao_formato": VERSAO_FORMATO,
        "limite": limite,
        "tabelas": tabelas,
        "resumo": {
            "aprovadas": len(aprovadas),
            **{s: contagem.get(s, 0) for s in SITUACOES},
            "nao_lidas": len(set(aprovadas) - set(resultados)),
            "para_revisao": sum(1 for r in resultados.values() if r["revisar"]),
        },
    }


def secao_duvidas(resultados: dict[str, dict[str, Any]]) -> str:
    """Seção gerenciada de `duvidas.md`: só nomes de tabela e coluna, nunca valores."""
    linhas = [MARCA_INICIO, "## Valores de domínio com dado sensível detectado (tarefa 3.2)", ""]
    linhas.append(
        "O detector de dados pessoais e credenciais mascarou valores nestas tabelas de domínio. "
        "Confirmar se a tabela deveria mesmo estar em `config/dominios.yml`. Detalhe: `catalogo/dominios/`."
    )
    linhas.append("")
    for tabela, r in sorted(resultados.items()):
        if r["revisar"]:
            colunas = sorted({m["coluna"] for m in r["mascaramentos"]})
            tipos = sorted({m["tipo"] for m in r["mascaramentos"]})
            linhas.append(f"- `{tabela}`: colunas {', '.join(f'`{c}`' for c in colunas)}; tipos detectados: {', '.join(tipos)}.")
    linhas.append(MARCA_FIM)
    return "\n".join(linhas) + "\n"


def atualizar_duvidas(doc: Path, resultados: dict[str, dict[str, Any]]) -> None:
    """Reescreve a seção gerenciada de `duvidas.md` (remove-a quando não há tabela para revisar)."""
    caminho = doc / "duvidas.md"
    atual = caminho.read_text(encoding="utf-8") if caminho.is_file() else "# Dúvidas\n\nO que a validação não resolveu. Cada item indica o caminho do detalhe.\n"
    padrao = re.compile(re.escape(MARCA_INICIO) + r".*?" + re.escape(MARCA_FIM) + r"\n?", re.DOTALL)
    sem_secao = padrao.sub("", atual).rstrip("\n") + "\n"
    revisar = any(r["revisar"] for r in resultados.values())
    novo = sem_secao + ("\n" + secao_duvidas(resultados) if revisar else "")
    if novo != atual or not caminho.is_file():
        with open(caminho, "w", encoding="utf-8", newline="\n") as arquivo:
            arquivo.write(novo)


# ----------------------------------------------------------------- execução


def gravar_indice(doc: Path, aprovadas: Collection[str], limite: int) -> dict[str, Any]:
    resultados = ler_resultados(doc, aprovadas)
    indice = montar_indice(aprovadas, resultados, limite)
    validar(indice, "dominio-indice.schema.json", "índice de domínios")
    escrever_json(pasta_saida(doc) / ARQ_INDICE, indice)
    atualizar_duvidas(doc, resultados)
    return indice


def ler_dominios(
    doc: Path,
    conexao: Executor,
    estruturas: dict[str, dict[str, Any]],
    aprovadas: set[str],
    *,
    limite: int = LIMITE_LINHAS,
    pausa_s: float = PAUSA_PADRAO_S,
    refazer: bool = False,
    dormir: Callable[[float], None] = time.sleep,
    avisar: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Lê as tabelas aprovadas ainda não resolvidas, grava um arquivo por tabela e o índice.

    O índice é regravado mesmo se a leitura for interrompida (perda de conexão, Ctrl+C).
    """
    if any(t.count(".") < 1 or any(c in t for c in '<>:"/\\|?*') for t in aprovadas):
        raise ErroDominios("config/dominios.yml: use nomes schema.tabela, sem caracteres inválidos em nome de arquivo.")
    relacoes = {f"{s}.{r['nome']}": r for s, d in estruturas.items() for r in d.get("relacoes", [])}
    pasta = pasta_saida(doc)
    feitas = ler_resultados(doc, aprovadas)
    pendentes = [t for t in sorted(aprovadas) if refazer or feitas.get(t, {}).get("situacao") not in SITUACOES_FINAIS]
    avisar(f"Domínios: {len(aprovadas)} aprovadas, {len(aprovadas) - len(pendentes)} já resolvidas, {len(pendentes)} a ler.")
    try:
        for i, tabela in enumerate(pendentes):
            if i:
                dormir(pausa_s)
            resultado = ler_tabela(tabela, relacoes.get(tabela), conexao, aprovadas, limite)
            validar(resultado, "dominio-tabela.schema.json", f"resultado de {tabela}")
            escrever_json(pasta / nome_arquivo(tabela), resultado)
            if (i + 1) % 25 == 0:
                avisar(f"  {i + 1}/{len(pendentes)} tabelas lidas.")
    except ErroConexao:
        avisar("Conexão perdida; o índice reflete o que foi gravado. Rode de novo para retomar.")
        raise
    finally:
        indice = gravar_indice(doc, aprovadas, limite)
    return indice


# ----------------------------------------------------------------- CLI


def registrar(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("dominios", help="valores das tabelas de domínio aprovadas (conecta ao banco)")
    etapas = parser.add_subparsers(dest="etapa", metavar="etapa", required=True)
    p = etapas.add_parser("ler", help="lê as tabelas de config/dominios.yml e grava catalogo/dominios/")
    adicionar_argumentos_comuns(p)
    p.add_argument("--pausa", type=float, default=PAUSA_PADRAO_S, help="segundos entre consultas (padrão 0,5)")
    p.add_argument("--refazer", action="store_true", help="relê também as tabelas já resolvidas")
    p.set_defaults(func=executar)


def executar(args: argparse.Namespace) -> int:
    from pg_atlas.conexao import Conexao  # import tardio: só este comando conecta
    from pg_atlas.relacionamentos import carregar_estruturas

    projeto = carregar_projeto(args.doc, args.workspace)
    aprovadas = ler_dominios_aprovados(projeto.doc)
    if not aprovadas:
        raise ErroDominios("config/dominios.yml sem tabelas aprovadas; nada a ler.")
    estruturas = carregar_estruturas(projeto.doc / "catalogo")
    with Conexao(projeto) as conexao:
        indice = ler_dominios(projeto.doc, conexao, estruturas, aprovadas, pausa_s=max(0.0, args.pausa), refazer=args.refazer)
    r = indice["resumo"]
    print(
        f"Domínios: lidas {r['lida']}, truncadas {r['truncada']}, sem privilégio {r['sem_privilegio']}, "
        f"erro {r['erro']}, não lidas {r['nao_lidas']}; para revisão (duvidas.md): {r['para_revisao']}."
    )
    return 0 if r["nao_lidas"] == 0 else 4
