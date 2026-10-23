"""Comando `relatorio catalogo`: relatório da fase 1 (tarefa 1.6 do plano, decisão D-015).

Não conecta ao banco: lê só arquivos já gerados (catálogo mascarado, triagem, verificação,
mascaramento, marcas de fase e consumo). Grava `<doc>/relatorios/fase-1-catalogo.md`.

O conteúdo gerado fica entre marcadores `pg-atlas:auto` (formato da D-013). A seção
"Observações" fica fora dos marcadores: é escrita só na criação e nunca é reescrita.
Nenhum valor de dado entra no relatório: só contagens, tamanhos e nomes de catálogo.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pg_atlas import custos
from pg_atlas.cli import adicionar_argumentos_comuns
from pg_atlas.config import carregar_projeto
from pg_atlas.erros import ErroAtlas
from pg_atlas.esqueleto_formato import linhas_arredondadas, milhar, percentual, tabela, tamanho_legivel
from pg_atlas.esqueleto_marcadores import atualizar, envolver, neutralizar
from pg_atlas.triagem import escrever_texto, somar_particoes

FASE = 1
DESTINO = "relatorios/fase-1-catalogo.md"
TOP_MAIORES = 20
TIPOS_RANKING = ("tabela", "particionada", "materializada")
SECOES = (
    "cabecalho", "banco", "triagem", "maiores", "sem_acesso",
    "mascaramento", "verificacao", "tempo", "custo",
)  # fmt: skip
TEXTO_OBSERVACOES = (
    "## Observações\n\n"
    "_Texto escrito à mão (só metadado, sem valores de dado). O comando `relatorio catalogo` "
    "nunca reescreve esta seção._"
)
LISTA_ACESSO = (
    "A lista completa está no campo `acesso` de `catalogo/_triagem.json`, na estrutura em "
    "`catalogo/estrutura/` e no cabeçalho das fichas."
)


class ErroRelatorio(ErroAtlas):
    """Entrada ausente ou ilegível."""

    codigo_saida = 4


Chave = tuple[str, str]


@dataclass
class Entradas:
    """Tudo o que o relatório lê, já carregado."""

    nome_projeto: str
    banco: dict[str, Any]
    estruturas: dict[str, dict[str, Any]]
    estatisticas: dict[str, dict[str, Any]]
    triagem: dict[str, Any]
    verificacao: dict[str, Any]
    mascaramento: dict[str, Any]
    marcas: list[dict[str, Any]]
    consumo: dict[str, Any]
    precos: dict[str, custos.Preco] = field(default_factory=dict)
    cambio: custos.Cambio | None = None


# --------------------------------------------------------------------------- leitura


def _ler_json(caminho: Path, dica: str) -> dict[str, Any]:
    if not caminho.is_file():
        raise ErroRelatorio(f"Arquivo ausente: {caminho}. {dica}")
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ErroRelatorio(f"Não foi possível ler {caminho} como JSON. {dica}") from None
    if not isinstance(dados, dict):
        raise ErroRelatorio(f"{caminho} deveria conter um objeto JSON. {dica}")
    return dados


def carregar_entradas(doc: Path, nome_projeto: str) -> Entradas:
    """Lê todos os arquivos de entrada; falta de qualquer um é erro com instrução."""
    cat, ctl = doc / "catalogo", doc / "_controle"
    banco = _ler_json(cat / "_banco.json", "Rode 'pg_atlas catalogo'.")
    schemas = sorted(banco.get("schemas") or {})
    estruturas, estatisticas = {}, {}
    for s in schemas:
        estruturas[s] = _ler_json(cat / "estrutura" / f"{s}.json", "Rode 'pg_atlas catalogo'.")
        estatisticas[s] = _ler_json(cat / "estatisticas" / f"{s}.json", "Rode 'pg_atlas catalogo'.")
    fases = custos.arquivo_fases(doc)
    if not fases.is_file():
        raise ErroRelatorio(f"Arquivo ausente: {fases}. Registre as marcas com 'pg_atlas custos marcar-inicio'.")
    return Entradas(
        nome_projeto=nome_projeto,
        banco=banco,
        estruturas=estruturas,
        estatisticas=estatisticas,
        triagem=_ler_json(cat / "_triagem.json", "Rode 'pg_atlas triagem'."),
        verificacao=_ler_json(ctl / "verificacao-catalogo.json", "Rode 'pg_atlas verificar catalogo'."),
        mascaramento=_ler_json(ctl / "mascaramento.json", "Rode 'pg_atlas catalogo'."),
        marcas=custos.ler_marcas(fases),
        consumo=_ler_json(custos.arquivo_consumo(doc), "Rode 'pg_atlas custos coletar'."),
        precos=custos.carregar_precos(doc),
        cambio=custos.carregar_cambio(doc),
    )


# --------------------------------------------------------------------------- seções


def _inteiro(valor: Any) -> int:
    return valor if isinstance(valor, int) and not isinstance(valor, bool) else 0


def secao_cabecalho(e: Entradas) -> str:
    return "\n".join(
        [
            "# Relatório da fase 1: catálogo",
            "",
            f"**Projeto:** {e.nome_projeto or custos.NAO_INFORMADO}",
            f"**Catálogo extraído em:** {e.banco.get('extraido_em') or custos.NAO_INFORMADO}",
        ]
    )


def _contar_rotinas(e: Entradas) -> dict[str, int]:
    contagem: dict[str, int] = {}
    for doc in e.estruturas.values():
        for f in doc.get("funcoes", []):
            contagem[f.get("tipo", "?")] = contagem.get(f.get("tipo", "?"), 0) + 1
    return contagem


def _contar_particoes(e: Entradas) -> int:
    return sum(1 for d in e.estruturas.values() for r in d.get("relacoes", []) if r.get("particao"))


def _versao_servidor(e: Entradas) -> str:
    achou = re.match(r"PostgreSQL \S+", str(e.banco.get("versao") or ""))
    return achou.group(0) if achou else custos.NAO_INFORMADO


def secao_banco(e: Entradas) -> str:
    t = e.banco.get("totais") or {}
    rotinas = _contar_rotinas(e)
    outras = sum(n for tipo, n in rotinas.items() if tipo not in ("function", "procedure"))
    linhas = [
        ("Schemas", _inteiro(e.banco.get("total_schemas")) or len(e.estruturas)),
        (f"Tabelas (inclui {milhar(_contar_particoes(e))} partições)", _inteiro(t.get("tabela"))),
        ("Tabelas particionadas", _inteiro(t.get("particionada"))),
        ("Views", _inteiro(t.get("view"))),
        ("Views materializadas", _inteiro(t.get("materializada"))),
        ("Tabelas estrangeiras", _inteiro(t.get("estrangeira"))),
        ("Funções", rotinas.get("function", 0)),
        ("Procedimentos (procedures)", rotinas.get("procedure", 0)),
        ("Agregações e outras rotinas", outras),
        ("Gatilhos", _inteiro(t.get("gatilhos"))),
        ("Sequências", _inteiro(t.get("sequencias"))),
        ("Índices", _inteiro(t.get("indices"))),
        ("Restrições", _inteiro(t.get("restricoes"))),
        ("Colunas", _inteiro(t.get("colunas"))),
        ("Tipos definidos", _inteiro(t.get("tipos"))),
    ]
    return "\n".join(
        [
            "## Números do banco",
            "",
            f"Servidor: {_versao_servidor(e)}. Tamanho total do banco: "
            f"{tamanho_legivel(e.banco.get('tamanho_bytes'))}.",
            "",
            tabela(["Item", "Quantidade"], [(rotulo, milhar(n)) for rotulo, n in linhas]),
        ]
    )


def secao_triagem(e: Entradas) -> str:
    resumo = e.triagem.get("resumo") or {}
    por_classe: dict[str, int] = resumo.get("por_classe") or {}
    ordem = list((e.triagem.get("criterios") or {}).get("precedencia") or sorted(por_classe))
    ordem += sorted(c for c in por_classe if c not in ordem)
    total = sum(por_classe.values())
    linhas = [
        (c, milhar(por_classe.get(c, 0)), percentual(por_classe.get(c, 0) / total if total else None))
        for c in ordem
    ]
    linhas.append(("**Total**", milhar(total), percentual(1.0 if total else None)))
    return "\n".join(
        [
            "## Distribuição da triagem",
            "",
            "Classes na ordem de precedência (primeira regra que casa). Detalhe por schema e "
            "critérios em `catalogo/_triagem.json`.",
            "",
            tabela(["Classe", "Tabelas", "Parte"], linhas),
        ]
    )


def _tamanhos_e_linhas(e: Entradas) -> tuple[dict[Chave, tuple[int | None, int | None]], dict[Chave, dict[str, Any]]]:
    """(linhas estimadas, tamanho total) próprios de cada relação, e a estrutura de cada uma."""
    proprias: dict[Chave, tuple[int | None, int | None]] = {}
    for schema, doc in e.estatisticas.items():
        for r in doc.get("relacoes", []):
            proprias[(schema, r["nome"])] = (_positivo(r.get("linhas_estimadas")), _positivo(r.get("tamanho_total_bytes")))
    estruturas = {(s, r["nome"]): r for s, d in e.estruturas.items() for r in d.get("relacoes", [])}
    return proprias, estruturas


def _positivo(valor: Any) -> int | None:
    if isinstance(valor, bool) or not isinstance(valor, (int, float)) or valor < 0:
        return None
    return int(round(valor))


def ranking_maiores(e: Entradas, limite: int = TOP_MAIORES) -> list[dict[str, Any]]:
    """Top por tamanho total; particionada soma as partições, que ficam fora do ranking."""
    proprias, estruturas = _tamanhos_e_linhas(e)
    filhas: dict[Chave, list[Chave]] = {}
    for chave, r in sorted(estruturas.items()):
        p = r.get("particao")
        if p:
            filhas.setdefault((p.get("pai_schema") or chave[0], p["pai_nome"]), []).append(chave)
    itens = []
    for chave, r in estruturas.items():
        if r.get("particao") or r.get("tipo") not in TIPOS_RANKING:
            continue
        linhas, tamanho = somar_particoes(chave, proprias, filhas)
        itens.append({"schema": chave[0], "nome": chave[1], "tipo": r["tipo"], "tamanho": tamanho, "linhas": linhas})
    itens.sort(key=lambda i: (-(i["tamanho"] or 0), i["schema"], i["nome"]))
    return itens[:limite]


def secao_maiores(e: Entradas) -> str:
    itens = ranking_maiores(e)
    linhas = [
        (n, i["schema"], i["nome"], i["tipo"], tamanho_legivel(i["tamanho"]), linhas_arredondadas(i["linhas"]))
        for n, i in enumerate(itens, start=1)
    ]
    corpo = tabela(["#", "Schema", "Tabela", "Tipo", "Tamanho total", "Linhas estimadas"], linhas)
    return "\n".join(
        [
            f"## Maiores tabelas (top {TOP_MAIORES})",
            "",
            "Por tamanho total em disco (tabela, índices e TOAST). Tabela particionada entra com a "
            "soma das suas partições; as partições ficam fora do ranking. Linhas estimadas, "
            "arredondadas a 2 algarismos significativos.",
            "",
            corpo,
        ]
    )


def _tabela_sem_acesso(titulo: str, contagem: dict[tuple[str, str], int], coluna: str) -> list[str]:
    if not contagem:
        return [f"### {titulo}", "", "Nenhum."]
    linhas = [(s, t, milhar(n)) for (s, t), n in sorted(contagem.items())]
    linhas.append(("**Total**", "", milhar(sum(contagem.values()))))
    return [f"### {titulo}", "", tabela(["Schema", coluna, "Quantidade"], linhas)]


def contar_sem_acesso(e: Entradas) -> tuple[dict[tuple[str, str], int], dict[tuple[str, str], int]]:
    """(relações sem SELECT, rotinas sem EXECUTE), por (schema, tipo)."""
    relacoes: dict[tuple[str, str], int] = {}
    rotinas: dict[tuple[str, str], int] = {}
    for schema, doc in e.estruturas.items():
        for r in doc.get("relacoes", []):
            if r.get("acesso") == "sem_select":
                chave = (schema, str(r.get("tipo")))
                relacoes[chave] = relacoes.get(chave, 0) + 1
        for f in doc.get("funcoes", []):
            if f.get("acesso") == "sem_execute":
                chave = (schema, str(f.get("tipo")))
                rotinas[chave] = rotinas.get(chave, 0) + 1
    return relacoes, rotinas


def secao_sem_acesso(e: Entradas) -> str:
    relacoes, rotinas = contar_sem_acesso(e)
    return "\n".join(
        [
            "## Objetos sem acesso",
            "",
            "Só contagens. " + LISTA_ACESSO,
            "",
            *_tabela_sem_acesso("Relações sem permissão de SELECT", relacoes, "Tipo"),
            "",
            *_tabela_sem_acesso("Rotinas sem permissão de EXECUTE", rotinas, "Tipo"),
        ]
    )


def secao_mascaramento(e: Entradas) -> str:
    totais: dict[str, int] = {}
    for r in e.mascaramento.get("registros", []):
        totais[str(r.get("tipo"))] = totais.get(str(r.get("tipo")), 0) + _inteiro(r.get("quantidade"))
    linhas = [(t, milhar(n)) for t, n in sorted(totais.items())]
    linhas.append(("**Total**", milhar(sum(totais.values()))))
    corpo = tabela(["Tipo", "Ocorrências mascaradas"], linhas) if totais else "Nenhuma ocorrência."
    return "\n".join(
        [
            "## Mascaramento aplicado ao catálogo",
            "",
            "Totais por tipo, a partir de `_controle/mascaramento.json`. Nenhum valor original "
            "é registrado.",
            "",
            corpo,
        ]
    )


def secao_verificacao(e: Entradas) -> str:
    v = e.verificacao
    linhas = []
    for item in v.get("verificacoes", []):
        resumo = ", ".join(f"{k}={milhar(n) if isinstance(n, int) else n}" for k, n in sorted((item.get("resumo") or {}).items()))
        linhas.append((item.get("nome", "?"), "OK" if item.get("ok") else "FALHA", resumo or "—"))
    corpo = tabela(["Verificação", "Resultado", "Resumo"], linhas) if linhas else "Nenhuma verificação registrada."
    return "\n".join(
        [
            "## Verificação do catálogo",
            "",
            f"Resultado geral: {'OK' if v.get('ok') else 'FALHA'}. Contagem confrontada com o banco: "
            f"{'sim' if v.get('banco_consultado') else 'não'}. Verificado em {v.get('verificado_em') or custos.NAO_INFORMADO}.",
            "",
            corpo,
        ]
    )


# --------------------------------------------------------------------------- tempo


def _instante(valor: datetime) -> str:
    return valor.strftime("%Y-%m-%d %H:%M")


def _duracao(inicio: datetime, fim: datetime) -> str:
    minutos = int((fim - inicio).total_seconds() // 60)
    horas, resto = divmod(max(minutos, 0), 60)
    return f"{horas} h {resto:02d} min" if horas else f"{resto} min"


def periodo_das_marcas(
    marcas: list[dict[str, Any]], todas: list[dict[str, Any]] | None = None
) -> tuple[datetime, datetime, bool] | None:
    """(primeiro início, fim, em andamento). Fim é a última marca `fim`; em andamento
    (sem `fim`, ou com `inicio` depois do último `fim`), é a última marca de `todas`
    (padrão: as próprias `marcas`)."""
    inicios = [m["quando"] for m in marcas if m["evento"] == "inicio"]
    fins = [m["quando"] for m in marcas if m["evento"] == "fim"]
    if not inicios:
        return None
    ultima = max(m["quando"] for m in (todas or marcas))
    if not fins or max(inicios) > max(fins):
        return min(inicios), ultima, True
    return min(inicios), max(fins), False


def _horas_consumo(balde: dict[str, Any] | None) -> str:
    if not balde or "horas" not in balde:
        return "—"
    return custos._fmt_dec(float(balde["horas"]))


def _linha_tempo(
    rotulo: str, marcas: list[dict[str, Any]], balde: dict[str, Any] | None,
    todas: list[dict[str, Any]] | None = None,
) -> tuple[str, ...]:
    p = periodo_das_marcas(marcas, todas)
    if p is None:
        return (rotulo, "—", "sem marcas", "—", _horas_consumo(balde))
    inicio, fim, andamento = p
    fim_txt = f"em andamento até {_instante(fim)}" if andamento else _instante(fim)
    return (rotulo, _instante(inicio), fim_txt, _duracao(inicio, fim), _horas_consumo(balde))


def secao_tempo(e: Entradas) -> str:
    da_fase = [m for m in e.marcas if m["fase"] == FASE]
    fase_consumo = (e.consumo.get("fases") or {}).get(str(FASE)) or {}
    tarefas_consumo = fase_consumo.get("tarefas") or {}
    linhas = [_linha_tempo(f"Fase {FASE}", [m for m in da_fase if m["tarefa"] is None] or da_fase, fase_consumo, da_fase)]
    tarefas = sorted({m["tarefa"] for m in da_fase if m["tarefa"]}, key=_chave_tarefa)
    for t in tarefas:
        linhas.append(_linha_tempo(f"Tarefa {t}", [m for m in da_fase if m["tarefa"] == t], tarefas_consumo.get(t)))
    extraido = e.banco.get("extraido_em") or custos.NAO_INFORMADO
    return "\n".join(
        [
            "## Tempo de execução",
            "",
            "Tempo de relógio, das marcas de `_controle/custos/fases.jsonl` (primeiro início até o "
            "último fim; marcas de fim repetidas valem pela última). Horas de execução são as de "
            "`consumo.json`: aproximação por sessão, que inclui pausas dentro da sessão.",
            "",
            tabela(["Item", "Início", "Fim", "Tempo de relógio", "Horas de execução"], linhas),
            "",
            f"Tempo da extração do catálogo: não registrado (o catálogo guarda só o instante final: {extraido}).",
        ]
    )


def _chave_tarefa(tarefa: str) -> tuple[int, ...]:
    return tuple(int(p) if p.isdigit() else 0 for p in tarefa.split("."))


# --------------------------------------------------------------------------- custo


def secao_custo(e: Entradas) -> str:
    balde = (e.consumo.get("fases") or {}).get(str(FASE)) or {}
    modelos: dict[str, dict[str, int]] = balde.get("modelos") or {}
    linhas = []
    for modelo in sorted(modelos):
        t = modelos[modelo]
        linhas.append(_linha_modelo(modelo, t, e.precos))
    total = custos._soma_tokens(modelos)
    linhas.append(
        (
            "**Total**", milhar(total["entrada"]), milhar(total["saida"]),
            milhar(total["cache_escrita_5m"] + total["cache_escrita_1h"]), milhar(total["cache_leitura"]),
            custos._fmt_usd(custos._custo_balde(modelos, e.precos)),
        )
    )  # fmt: skip
    corpo = (
        tabela(["Modelo", "Entrada", "Saída", "Cache (escrita)", "Cache (leitura)", "Equivalente US$"], linhas)
        if modelos
        else "Nenhum consumo registrado para a fase."
    )
    periodo = (e.consumo.get("periodo") or {}).get("fim") or custos.NAO_INFORMADO
    premissas = [
        f"- Consumo coletado até {periodo} (`custos coletar`); inclui só o que foi atribuído à fase {FASE} pelas marcas.",
        "- Custo equivalente em API: tokens × preços de `config/precos.yml`. Não é o valor pago.",
        "- Relatório parcial completo, com as tarefas: `relatorios/custo-parcial-fase-1.md`.",
    ]
    sem_preco = sorted(m for m, t in modelos.items() if custos.custo_usd(m, t, e.precos) is None)
    if sem_preco:
        premissas.append(
            "- AVISO: preço não informado em `config/precos.yml` para: " + ", ".join(f"`{m}`" for m in sem_preco) + "."
        )
    if e.cambio is None:
        premissas.append("- AVISO: câmbio não informado em `config/cambio.yml`: valores em reais indisponíveis.")
    return "\n".join(["## Custo da fase", "", corpo, "", *premissas])


def _linha_modelo(modelo: str, t: dict[str, int], precos: dict[str, custos.Preco]) -> tuple[str, ...]:
    return (
        modelo, milhar(t.get("entrada", 0)), milhar(t.get("saida", 0)),
        milhar(t.get("cache_escrita_5m", 0) + t.get("cache_escrita_1h", 0)), milhar(t.get("cache_leitura", 0)),
        custos._fmt_usd(custos.custo_usd(modelo, t, precos)),
    )  # fmt: skip


# --------------------------------------------------------------------------- montagem


CONSTRUTORES = {
    "cabecalho": secao_cabecalho,
    "banco": secao_banco,
    "triagem": secao_triagem,
    "maiores": secao_maiores,
    "sem_acesso": secao_sem_acesso,
    "mascaramento": secao_mascaramento,
    "verificacao": secao_verificacao,
    "tempo": secao_tempo,
    "custo": secao_custo,
}


def montar_secoes(e: Entradas) -> list[tuple[str, str]]:
    """Seções automáticas, na ordem canônica, com o conteúdo neutralizado."""
    return [(nome, neutralizar(CONSTRUTORES[nome](e))) for nome in SECOES]


def montar_novo(secoes: list[tuple[str, str]]) -> str:
    """Arquivo novo: seções automáticas e a seção Observações fixa no fim."""
    partes = [envolver(nome, texto) for nome, texto in secoes] + [TEXTO_OBSERVACOES]
    return "\n\n".join(partes) + "\n"


def gerar_relatorio(doc: Path, nome_projeto: str) -> str:
    """Cria o relatório ou reescreve só as seções automáticas. Devolve `criado`,
    `atualizado` ou `inalterado`. Marcadores quebrados levantam `ErroMarcadores`."""
    secoes = montar_secoes(carregar_entradas(doc, nome_projeto))
    destino = doc / DESTINO
    if not destino.exists():
        escrever_texto(destino, montar_novo(secoes))
        return "criado"
    atual = destino.read_text(encoding="utf-8-sig")
    novo, _ = atualizar(atual, secoes)
    if novo == atual:
        return "inalterado"
    escrever_texto(destino, novo)
    return "atualizado"


# --------------------------------------------------------------------------- CLI


def registrar(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("relatorio", help="relatórios de fase (catálogo), sem conectar ao banco")
    alvos = parser.add_subparsers(dest="alvo", metavar="alvo", required=True)
    catalogo_parser = alvos.add_parser("catalogo", help="gera relatorios/fase-1-catalogo.md a partir dos arquivos da fase 1")
    adicionar_argumentos_comuns(catalogo_parser)
    catalogo_parser.set_defaults(func=executar)


def executar(args: argparse.Namespace) -> int:
    projeto = carregar_projeto(args.doc, args.workspace)
    situacao = gerar_relatorio(projeto.doc, projeto.nome)
    print(f"Relatório da fase 1 {situacao}: {projeto.doc / DESTINO}")
    return 0
