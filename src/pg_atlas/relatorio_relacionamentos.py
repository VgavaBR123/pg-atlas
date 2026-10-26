"""Alvo `relatorio relacionamentos`: relatório da fase 2 (tarefa 2.7, decisão D-022).

Não conecta ao banco: lê só arquivos já gerados (grafo, lotes, catálogo, triagem, marcas e consumo).
Grava `<doc>/relatorios/fase-2-relacionamentos.md` no mesmo padrão do relatório da fase 1: conteúdo gerado
entre marcadores `pg-atlas:auto` e seção "Observações" fora deles, escrita só na criação e nunca reescrita.
Só contagens, taxas e nomes de catálogo: nenhum valor de dado entra no relatório.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pg_atlas import custos
from pg_atlas.cli import adicionar_argumentos_comuns
from pg_atlas.config import carregar_projeto
from pg_atlas.esqueleto_formato import milhar, percentual, tabela
from pg_atlas.esqueleto_marcadores import atualizar, envolver, neutralizar
from pg_atlas.relacionamentos import carregar_estruturas
from pg_atlas.relacionamentos_agrupar import (
    SAIDA_LOTES,
    carregar_grafo,
    carregar_triagem,
    criterios_de_aceite,
)
from pg_atlas.relatorio_fase import ErroRelatorio, _ler_json, secao_custo, secao_tempo
from pg_atlas.triagem import escrever_texto

FASE = 2
DESTINO = "relatorios/fase-2-relacionamentos.md"
PASTA_PENDENTES = ("_controle", "verificacoes", "pendentes")
SECOES = ("cabecalho", "grafo", "amostragem", "analista", "lotes", "criterios", "pendencias", "tempo", "custo")
FAIXAS = ((1, 1), (2, 5), (6, 10), (11, 15), (16, 20), (21, 40), (41, 10**9))
TEXTO_OBSERVACOES = (
    "## Observações\n\n"
    "_Texto escrito à mão (só metadado, sem valores de dado). O comando `relatorio relacionamentos` "
    "nunca reescreve esta seção._"
)


@dataclass
class Entradas:
    """Tudo o que o relatório lê. Os nomes `banco`, `marcas`, `consumo`, `precos` e `cambio` são os que
    `secao_tempo` e `secao_custo` da fase 1 esperam."""

    nome_projeto: str
    banco: dict[str, Any]
    grafo: dict[str, Any]
    lotes: dict[str, Any]
    triagem: list[dict[str, Any]]
    estruturas: dict[str, dict[str, Any]]
    verificacoes_pendentes: int
    marcas: list[dict[str, Any]]
    consumo: dict[str, Any]
    precos: dict[str, custos.Preco] = field(default_factory=dict)
    cambio: custos.Cambio | None = None


def carregar_entradas(doc: Path, nome_projeto: str) -> Entradas:
    lotes_arquivo = doc.joinpath(*SAIDA_LOTES)
    fases = custos.arquivo_fases(doc)
    if not fases.is_file():
        raise ErroRelatorio(f"Arquivo ausente: {fases}. Registre as marcas com 'pg_atlas custos marcar-inicio'.")
    pendentes = doc.joinpath(*PASTA_PENDENTES)
    return Entradas(
        nome_projeto=nome_projeto,
        banco=_ler_json(doc / "catalogo" / "_banco.json", "Rode 'pg_atlas catalogo'."),
        grafo=carregar_grafo(doc),
        lotes=_ler_json(lotes_arquivo, "Rode 'pg_atlas relacionamentos agrupar'."),
        triagem=carregar_triagem(doc),
        estruturas=carregar_estruturas(doc / "catalogo"),
        verificacoes_pendentes=len(list(pendentes.glob("*.sql"))) if pendentes.is_dir() else 0,
        marcas=custos.ler_marcas(fases),
        consumo=_ler_json(custos.arquivo_consumo(doc), "Rode 'pg_atlas custos coletar'."),
        precos=custos.carregar_precos(doc),
        cambio=custos.carregar_cambio(doc),
    )


# --------------------------------------------------------------------------- seções


def secao_cabecalho(e: Entradas) -> str:
    return "\n".join(
        [
            "# Relatório da fase 2: relacionamentos",
            "",
            f"**Projeto:** {e.nome_projeto or custos.NAO_INFORMADO}",
            f"**Catálogo extraído em:** {e.banco.get('extraido_em') or custos.NAO_INFORMADO}",
        ]
    )


def secao_grafo(e: Entradas) -> str:
    arestas = e.grafo["arestas"]
    r = e.grafo["resumo"]
    fontes = sorted({a["fonte"] for a in arestas})
    confs = ["declarado", "confirmado", "hipotese"]
    cont = Counter((a["fonte"], a["confianca"]) for a in arestas)
    linhas = [(f, *(milhar(cont[(f, c)]) for c in confs), milhar(sum(cont[(f, c)] for c in confs))) for f in fontes]
    linhas.append(
        ("**Total**", *(milhar(sum(cont[(f, c)] for f in fontes)) for c in confs), milhar(len(arestas)))
    )
    taxas = sorted(a["taxa_casamento"] for a in arestas if a["confianca"] == "confirmado" and a["taxa_casamento"] is not None)
    fk = r.get("fk_declarada") or {}
    ambiguas = sum(1 for a in arestas if "ambíguo" in (a.get("nota") or ""))
    itens = [
        f"- FKs declaradas: {milhar(fk.get('entre_schemas', 0))} entre schemas, "
        f"{milhar(fk.get('autorreferencias', 0))} autorreferências, {milhar(fk.get('varias_colunas', 0))} com várias "
        f"colunas, {milhar(fk.get('not_valid', 0))} NOT VALID.",
        f"- Arestas confirmadas com taxa de casamento registrada: {milhar(len(taxas))}"
        + (f" (menor taxa {percentual(taxas[0])}, maior {percentual(taxas[-1])})." if taxas else "."),
        f"- Arestas que ainda levam a nota de ambiguidade: {milhar(ambiguas)}.",
    ]
    nome = r.get("inferencia_nome") or {}
    codigo = r.get("inferencia_codigo") or {}
    if nome:
        regras = ", ".join(f"{k}={milhar(n)}" for k, n in sorted((nome.get("por_regra") or {}).items()))
        itens.append(f"- Inferência por nome: {milhar(nome.get('arestas', 0))} candidatos ({regras}).")
    if codigo:
        itens.append(
            f"- Inferência por código ({codigo.get('extrator', '?')}): {milhar(codigo.get('arestas', 0))} candidatos, "
            f"{milhar(sum((codigo.get('objetos_analisados') or {}).values()))} objetos analisados, "
            f"{milhar(codigo.get('execute_dinamico', 0))} comandos de SQL dinâmico só marcados."
        )
    return "\n".join(
        [
            "## Grafo de relacionamentos",
            "",
            "Arestas de `relacionamentos/grafo.json` por fonte e confiança.",
            "",
            tabela(["Fonte", *confs, "Total"], linhas),
            "",
            *itens,
        ]
    )


def secao_amostragem(e: Entradas) -> str:
    v = e.grafo["resumo"].get("verificacao")
    if not v:
        return "## Verificação por amostragem\n\nBloco `resumo.verificacao` ausente no grafo: rode `relacionamentos verificar --aplicar`."
    rotulos = [
        ("pares", "Pares candidatos"), ("verificados", "Verificados com amostra"), ("confirmadas", "Confirmados (95% ou mais)"),
        ("hipoteses", "Hipóteses (50% a 95%)"), ("descartadas", "Descartados (abaixo de 50%)"),
        ("amostra_vazia", "Sem amostra (coluna de origem vazia ou só nula)"), ("amostra_pequena", "Amostra pequena"),
        ("sem_select", "Sem permissão de SELECT"), ("desempatadas", "Ambíguos desempatados pela taxa"),
        ("not_valid_medidas", "FKs NOT VALID medidas"),
    ]
    linhas = [(rotulo, milhar(v[k])) for k, rotulo in rotulos if isinstance(v.get(k), int)]
    filtro = ", ".join(f"{k}={milhar(n)}" for k, n in sorted((v.get("por_filtro_tenant") or {}).items()))
    metodo = ", ".join(f"{k}={milhar(n)}" for k, n in sorted((v.get("por_metodo") or {}).items()))
    return "\n".join(
        [
            "## Verificação por amostragem",
            "",
            tabela(["Item", "Quantidade"], linhas),
            "",
            f"Filtro de tenant (por correlação, D-019 e D-020): {filtro or '—'}. Método: {metodo or '—'}. "
            f"Pares ainda pendentes de consulta: {milhar(v.get('pendentes', 0))}.",
        ]
    )


def secao_analista(e: Entradas) -> str:
    a = e.grafo["resumo"].get("analise")
    if not a:
        return "## Analista de relacionamentos\n\nBloco `resumo.analise` ausente no grafo: rode `relacionamentos resolver`."
    fila = a.get("colunas_fila") or {}
    lotes = a.get("lotes") or {}
    linhas = [
        ("Colunas na fila P1 (há taxa útil)", milhar(fila.get("p1", 0))),
        ("Colunas na fila P2 (outra evidência)", milhar(fila.get("p2", 0))),
        ("Colunas P3 (sem evidência, não vão ao analista)", milhar(fila.get("p3", 0))),
        ("Lotes gerados / resolvidos", f"{milhar(lotes.get('gerados', 0))} / {milhar(lotes.get('resolvidos', 0))}"),
        ("Colunas com destino escolhido", milhar(a.get("escolhidas", 0))),
        ("Colunas deixadas em dúvida", milhar(a.get("duvidas", 0))),
        ("Arestas preteridas (rebaixadas)", milhar(a.get("arestas_preteridas", 0))),
        ("Arestas descartadas", milhar(a.get("arestas_descartadas", 0))),
        ("Itens rejeitados na aplicação", milhar(a.get("itens_rejeitados", 0))),
    ]
    return "\n".join(["## Analista de relacionamentos", "", tabela(["Item", "Quantidade"], linhas)])


def _faixa(n: int) -> str:
    for ini, fim in FAIXAS:
        if ini <= n <= fim:
            return f"{ini}" if ini == fim else (f"{ini} ou mais" if fim >= 10**9 else f"{ini} a {fim}")
    return "?"


def distribuicao(lotes: list[dict[str, Any]], tipo: str) -> list[tuple[str, int]]:
    """(faixa de tamanho, quantidade de lotes), na ordem das faixas, sem as vazias."""
    contagem = Counter(_faixa(lote["total_tabelas"]) for lote in lotes if lote["tipo"] == tipo)
    return [(_faixa(ini), contagem[_faixa(ini)]) for ini, _ in FAIXAS if contagem[_faixa(ini)]]


def _numero(valor: float) -> str:
    return milhar(int(valor)) if float(valor).is_integer() else str(valor).replace(".", ",")


def secao_lotes(e: Entradas) -> str:
    L, r, p = e.lotes["lotes"], e.lotes["resumo"], e.lotes["parametros"]
    classes = r["tabelas_por_classe"]
    em_lote = Counter(t["classe"] for lote in L for t in lote["tabelas"])
    total = r["tabelas_total"]
    linhas_classe = [(c, milhar(n), milhar(em_lote.get(c, 0))) for c, n in sorted(classes.items())]
    linhas_classe.append(("**Total**", milhar(total), milhar(sum(em_lote.values()))))
    tam = r["tamanhos"]
    estat = [("Lotes", "lotes"), ("Menor (tabelas)", "minimo"), ("Mediana (tabelas)", "mediana"), ("Maior (tabelas)", "maximo")]
    linhas_tam = [(rot, *(_numero(tam[t][k]) if t in tam else "—" for t in ("grafo", "leve"))) for rot, k in estat]
    faixas_g, faixas_l = dict(distribuicao(L, "grafo")), dict(distribuicao(L, "leve"))
    nomes_faixas = [_faixa(ini) for ini, _ in FAIXAS if _faixa(ini) in faixas_g or _faixa(ini) in faixas_l]
    linhas_faixa = [(f, milhar(faixas_g.get(f, 0)), milhar(faixas_l.get(f, 0))) for f in nomes_faixas]
    a, c = r["arestas"], r["componentes"]
    return "\n".join(
        [
            "## Lotes para a fase 3",
            "",
            f"`relacionamentos/lotes.json`: {milhar(r['total_lotes'])} lotes ({milhar(r['lotes_grafo'])} do grafo, "
            f"{milhar(r['lotes_leves'])} leves). Algoritmo: {p['algoritmo']}, até {p['max_tabelas']} tabelas por lote do "
            f"grafo e {p['max_tabelas_leve']} por lote leve; arestas usadas: {', '.join(p['confiancas_usadas'])} (D-022).",
            "",
            "### Tabelas por classe",
            "",
            tabela(["Classe", "Tabelas na triagem", "Tabelas em lote"], linhas_classe),
            "",
            "### Tamanho dos lotes",
            "",
            tabela(["Item", "Lotes do grafo", "Lotes leves"], linhas_tam),
            "",
            tabela(["Tabelas no lote", "Lotes do grafo", "Lotes leves"], linhas_faixa),
            "",
            "### Grafo de agrupamento",
            "",
            f"- Arestas usadas: {milhar(a['usadas'])} ({', '.join(f'{k}={milhar(n)}' for k, n in a['usadas_por_confianca'].items())}).",
            f"- Ignoradas: {milhar(a['com_tabela_leve'])} com tabela `vazia` ou `temporaria_ou_copia` na ponta, "
            f"{milhar(a['autorreferencia'])} autorreferências, {milhar(a['fora_da_triagem'])} fora da triagem.",
            f"- Componentes conexos entre tabelas comuns, fatos e candidatas a domínio: {milhar(c['total'])} "
            f"(maior com {milhar(c['maior'])} tabelas, {milhar(c['isolados'])} isoladas).",
        ]
    )


def secao_criterios(e: Entradas) -> str:
    itens = criterios_de_aceite(e.estruturas, e.grafo, e.triagem, e.lotes)
    linhas = [(nome, "OK" if ok else "FALHA", detalhe) for nome, ok, detalhe in itens]
    return "\n".join(
        [
            "## Verificações de aceite",
            "",
            f"Resultado geral: {'OK' if all(ok for _, ok, _ in itens) else 'FALHA'}.",
            "",
            tabela(["Critério", "Resultado", "Detalhe"], linhas),
        ]
    )


def secao_pendencias(e: Entradas) -> str:
    r = e.grafo["resumo"]
    a = r.get("analise") or {}
    v = r.get("verificacao") or {}
    fila = a.get("colunas_fila") or {}
    linhas = [
        ("Verificações pendentes (SQL de agregado em `_controle/verificacoes/pendentes/`, não executadas)", milhar(e.verificacoes_pendentes)),
        ("Colunas P3 ambíguas sem evidência (`_controle/relacionamentos-sem-evidencia.json`)", milhar(fila.get("p3", a.get("sem_evidencia", 0)))),
        ("Colunas analisadas e deixadas em dúvida (`duvidas.md`)", milhar(a.get("duvidas", 0))),
        ("Pares sem amostra (coluna de origem vazia ou só nula)", milhar(v.get("amostra_vazia", 0))),
        ("Pares sem permissão de SELECT", milhar(v.get("sem_select", 0))),
        ("Pares com amostra pequena", milhar(v.get("amostra_pequena", 0))),
    ]
    return "\n".join(
        [
            "## Pendências",
            "",
            "Só contagens. O detalhe de cada item está no arquivo indicado.",
            "",
            tabela(["Pendência", "Quantidade"], linhas),
        ]
    )


def _tempo(e: Entradas) -> str:
    return secao_tempo(e, FASE, com_extracao=False).rstrip("\n")  # type: ignore[arg-type]


def _custo(e: Entradas) -> str:
    return secao_custo(e, FASE)  # type: ignore[arg-type]


CONSTRUTORES = {
    "cabecalho": secao_cabecalho,
    "grafo": secao_grafo,
    "amostragem": secao_amostragem,
    "analista": secao_analista,
    "lotes": secao_lotes,
    "criterios": secao_criterios,
    "pendencias": secao_pendencias,
    "tempo": _tempo,
    "custo": _custo,
}


def montar_secoes(e: Entradas) -> list[tuple[str, str]]:
    return [(nome, neutralizar(CONSTRUTORES[nome](e))) for nome in SECOES]


def montar_novo(secoes: list[tuple[str, str]]) -> str:
    partes = [envolver(nome, texto) for nome, texto in secoes] + [TEXTO_OBSERVACOES]
    return "\n\n".join(partes) + "\n"


def gerar_relatorio(doc: Path, nome_projeto: str) -> str:
    """Cria o relatório ou reescreve só as seções automáticas: `criado`, `atualizado` ou `inalterado`."""
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


def registrar(alvos: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = alvos.add_parser(
        "relacionamentos", help="gera relatorios/fase-2-relacionamentos.md a partir dos arquivos da fase 2"
    )
    adicionar_argumentos_comuns(parser)
    parser.set_defaults(func=executar)


def executar(args: argparse.Namespace) -> int:
    projeto = carregar_projeto(args.doc, args.workspace)
    situacao = gerar_relatorio(projeto.doc, projeto.nome)
    print(f"Relatório da fase 2 {situacao}: {projeto.doc / DESTINO}")
    return 0
