"""Conteúdo das fichas do esqueleto (decisão D-013, itens 2, 3, 8, 9 e 11).

Cada ficha é uma lista de blocos `(nome, texto)`: `nome` é o da seção automática
(entre marcadores) ou `None` para texto fixo (seções pendentes e Histórico), que só é
escrito quando o arquivo é criado. Só o conteúdo e a ordem das seções automáticas
valem na regeneração.
"""

from __future__ import annotations

from typing import Any

from pg_atlas.esqueleto_contexto import Chave, Contexto
from pg_atlas.esqueleto_formato import (
    TRAVESSAO,
    bloco_codigo_limitado,
    codigo_inline,
    distintos,
    linha_unica,
    linhas_arredondadas,
    link,
    percentual,
    tabela,
    tamanho_legivel,
)
from pg_atlas.esqueleto_marcadores import neutralizar
from pg_atlas.esqueleto_nomes import Objeto, caminho_readme

Bloco = tuple[str | None, str]
NENHUM = "Nenhum."
TIPOS_RELACAO = {
    "tabela": "tabela",
    "particionada": "tabela particionada",
    "view": "view",
    "materializada": "view materializada",
    "estrangeira": "tabela estrangeira",
}
TIPOS_ROTINA = {"function": "function", "procedure": "procedure", "aggregate": "agregado", "window": "função de janela"}
VOLATILIDADE = {"volatil": "volátil", "estavel": "estável", "imutavel": "imutável"}
ESPECIES_TIPO = {"enum": "enum", "composto": "composto", "dominio": "domínio"}
ORDEM_RESTRICAO = {"pk": 0, "uk": 1, "check": 2}
ROTULO_RESTRICAO = {"pk": "PK", "uk": "UK", "check": "CHECK"}
ORDEM_EVENTO = {"INSERT": 0, "UPDATE": 1, "DELETE": 2, "TRUNCATE": 3}
FRASE_SEM_SELECT = "Sem permissão de SELECT para a credencial: documentada só pela estrutura."
FRASE_SEM_EXECUTE = "Sem permissão de EXECUTE para a credencial: documentada só pela estrutura."
PARAGRAFO_CONFIANCA = (
    'Antes de escrever qualquer consulta, leia "Fatos essenciais". Toda afirmação nas fichas tem '
    "grau de confiança: declarado, confirmado ou hipotese. Prefira o que estiver confirmado."
)
AVISO_ESQUELETO = "Documentação em construção: esqueleto da fase 1, fichas só com estrutura."


# ----------------------------------------------------------------- blocos comuns


def pendente(titulo: str, fase: int) -> Bloco:
    texto = (
        f"## {titulo}\n\n"
        f'<!-- pg-atlas:pendente fase="{fase}" -->\n'
        f"_Pendente: a preencher na fase {fase}._"
    )
    return None, texto


def historico(ctx: Contexto) -> Bloco:
    return None, f"## Histórico\n\n- {ctx.extraido_em[:10]}: ficha criada pela fase 1."


def _auto(nome: str, texto: str) -> Bloco:
    return nome, neutralizar(texto)


def _comentario(comentario: str | None) -> str:
    if not comentario or not comentario.strip():
        return ""
    return f"\n\nComentário do banco: {linha_unica(comentario).strip()} [declarado]"


# ----------------------------------------------------------------- números da relação


def _linhas_da_relacao(ctx: Contexto, chave: Chave) -> int | None:
    item = ctx.triagem.get(chave)
    if item is not None:
        estimadas, vivas = item["linhas_estimadas"], item["linhas_vivas"]
    else:
        estat = ctx.estatisticas.get(chave) or {}
        estimadas, vivas = estat.get("linhas_estimadas"), estat.get("linhas_vivas")
    for valor in (estimadas, vivas):
        if isinstance(valor, int) and not isinstance(valor, bool) and valor >= 0:
            return valor
    return None


def _tamanho_da_relacao(ctx: Contexto, chave: Chave, visitadas: frozenset[Chave] = frozenset()) -> int | None:
    """Tamanho total; para particionada, a soma dela com as partições (todos os níveis)."""
    proprio = (ctx.estatisticas.get(chave) or {}).get("tamanho_total_bytes")
    partes = [proprio] if isinstance(proprio, int) else []
    for filha in ctx.filhas.get(chave, []):
        if filha not in visitadas:
            partes.append(_tamanho_da_relacao(ctx, filha, visitadas | {chave}))
    conhecidas = [p for p in partes if p is not None]
    return sum(conhecidas) if conhecidas else None


def _link_relacao(ctx: Contexto, origem: str, destino: Chave) -> str:
    texto = f"{destino[0]}.{destino[1]}"
    caminho = ctx.caminho_relacao.get(destino)
    return link(origem, caminho, texto) if caminho else texto


def _link_funcao(ctx: Contexto, origem: str, funcao: Objeto | None, alternativa: str) -> str:
    if funcao is None:
        return alternativa
    return link(origem, ctx.caminhos[funcao], alternativa)


# ----------------------------------------------------------------- ficha de relação


def secoes_da_relacao(ctx: Contexto, chave: Chave) -> list[str]:
    """Nomes das seções automáticas de uma relação, na ordem canônica."""
    r = ctx.relacoes[chave]
    if r.get("particao"):
        nomes = ["cabecalho", "indices", "rotinas"]
        return nomes + ["particoes"] if r["tipo"] == "particionada" else nomes
    nomes = ["cabecalho", "colunas", "relacionamentos", "indices", "rotinas", "dependencias"]
    if r["tipo"] in ("view", "materializada"):
        nomes.append("definicao")
    if r["tipo"] == "particionada":
        nomes.append("particoes")
    return nomes


def ficha_relacao(ctx: Contexto, chave: Chave) -> list[Bloco]:
    r = ctx.relacoes[chave]
    caminho = ctx.caminho_relacao[chave]
    gerar = {
        "cabecalho": lambda: _cabecalho_relacao(ctx, chave, caminho),
        "colunas": lambda: _secao_colunas(ctx, chave),
        "relacionamentos": lambda: _secao_relacionamentos(ctx, chave, caminho),
        "indices": lambda: _secao_indices(r),
        "rotinas": lambda: _secao_gatilhos(ctx, chave, caminho),
        "dependencias": lambda: _secao_dependencias(ctx, chave, caminho),
        "definicao": lambda: _secao_definicao(ctx, chave),
        "particoes": lambda: _secao_particoes(ctx, chave, caminho),
    }
    auto = {nome: _auto(nome, gerar[nome]()) for nome in secoes_da_relacao(ctx, chave)}
    if r.get("particao"):
        blocos = [auto["cabecalho"], (None, "Colunas e significado: ver a tabela-mãe.")]
        blocos += [auto[n] for n in secoes_da_relacao(ctx, chave)[1:]]
        return blocos + [historico(ctx)]
    sequencia: list[Bloco] = [auto["cabecalho"], pendente("Propósito", 3), pendente("Como usar", 3)]
    sequencia.append(auto["colunas"])
    sequencia.append(pendente("Significado das colunas", 3))
    nomes_restantes = secoes_da_relacao(ctx, chave)[2:]
    sequencia += [auto[n] for n in nomes_restantes]
    return sequencia + [pendente("Consultas típicas", 3), historico(ctx)]


def _cabecalho_relacao(ctx: Contexto, chave: Chave, caminho: str) -> str:
    r = ctx.relacoes[chave]
    partes = [_tipo_texto(r)]
    linhas = _linhas_da_relacao(ctx, chave)
    partes.append("linhas desconhecidas" if linhas is None else f"{linhas_arredondadas(linhas)} linhas (estimativa)")
    tamanho = _tamanho_da_relacao(ctx, chave)
    partes.append("tamanho desconhecido" if tamanho is None else tamanho_legivel(tamanho))
    item = ctx.triagem.get(chave)
    partes.append(f"triagem: {item['classe'] if item else 'não se aplica'}")
    sem_select = r.get("acesso") == "sem_select"
    partes.append("acesso: sem_select" if sem_select else "acesso: com_select")
    pai = r.get("particao")
    if pai and pai.get("pai_nome"):
        alvo = (pai.get("pai_schema") or chave[0], pai["pai_nome"])
        partes.append(f"partição de {_link_relacao(ctx, caminho, alvo)}")
    texto = f"# {chave[0]}.{chave[1]}\n\n> " + " · ".join(partes)
    if pai and pai.get("limite"):
        texto += f"\n> Limite da partição: {codigo_inline(pai['limite'])}"
    if r.get("chave_particao"):
        texto += f"\n> Chave de partição: {codigo_inline(r['chave_particao'])}"
    if sem_select:
        texto += f"\n\n{FRASE_SEM_SELECT}"
    return texto + _comentario(r.get("comentario"))


def _tipo_texto(r: dict[str, Any]) -> str:
    return "partição" if r.get("particao") else TIPOS_RELACAO.get(r["tipo"], r["tipo"])


def _estatisticas_das_colunas(ctx: Contexto, chave: Chave) -> dict[str, dict[str, Any]]:
    """Entrada não herdada de `pg_stats`; se faltar, a herdada."""
    por_nome: dict[str, dict[str, Any]] = {}
    for e in (ctx.estatisticas.get(chave) or {}).get("colunas", []):
        atual = por_nome.get(e["nome"])
        if atual is None or (atual["herdada"] and not e["herdada"]):
            por_nome[e["nome"]] = e
    return por_nome


def _marcas_de_chave(r: dict[str, Any]) -> dict[str, str]:
    marcas: dict[str, set[str]] = {}
    for restricao in r.get("restricoes", []):
        rotulo = {"pk": "PK", "uk": "UK", "fk": "FK"}.get(restricao["tipo"])
        if rotulo:
            for coluna in restricao["colunas"]:
                marcas.setdefault(coluna, set()).add(rotulo)
    ordem = {"PK": 0, "UK": 1, "FK": 2}
    return {c: "/".join(sorted(m, key=ordem.__getitem__)) for c, m in marcas.items()}


def _padrao_da_coluna(c: dict[str, Any]) -> str:
    if c.get("identidade"):
        return f"identity ({c['identidade']})"
    if c.get("gerada") and c.get("expressao_gerada"):
        return f"gerada: {codigo_inline(c['expressao_gerada'])}"
    return codigo_inline(c["padrao"]) if c.get("padrao") else TRAVESSAO


def _secao_colunas(ctx: Contexto, chave: Chave) -> str:
    r = ctx.relacoes[chave]
    estat = _estatisticas_das_colunas(ctx, chave)
    marcas = _marcas_de_chave(r)
    linhas = []
    for c in sorted(r.get("colunas", []), key=lambda c: c["ordem"]):
        e = estat.get(c["nome"])
        linhas.append([
            c["ordem"], c["nome"], codigo_inline(c["tipo"]), "não" if c["obrigatoria"] else "sim",
            _padrao_da_coluna(c), marcas.get(c["nome"], TRAVESSAO),
            percentual(e["null_frac"]) if e else TRAVESSAO, distintos(e["n_distinct"]) if e else TRAVESSAO,
        ])
    corpo = tabela(
        ["#", "Coluna", "Tipo", "Nulo", "Default", "Chave", "Nulos (%)", "Distintos"], linhas
    ) if linhas else NENHUM
    return f"## Colunas\n\n{corpo}"


def _secao_relacionamentos(ctx: Contexto, chave: Chave, caminho: str) -> str:
    r = ctx.relacoes[chave]
    saida = []
    for fk in sorted((x for x in r.get("restricoes", []) if x["tipo"] == "fk"), key=lambda x: x["nome"]):
        alvo = (fk["ref_schema"] or chave[0], fk["ref_relacao"] or "")
        destino = _link_relacao(ctx, caminho, alvo) + f"({', '.join(fk['ref_colunas'])})"
        saida.append([", ".join(fk["colunas"]), destino, "declarado", TRAVESSAO])
    entrada = [
        [_link_relacao(ctx, caminho, origem), ", ".join(cols), "declarado"]
        for origem, cols, _ in sorted(ctx.fk_entrada.get(chave, []), key=lambda x: (x[0], x[1]))
    ]
    de_saida = tabela(["Coluna(s)", "Tabela de destino", "Grau", "Casamento"], saida) if saida else NENHUM
    de_entrada = tabela(["Tabela de origem", "Coluna(s)", "Grau"], entrada) if entrada else NENHUM
    return (
        f"## Relacionamentos\n\n**Esta tabela aponta para:**\n\n{de_saida}\n\n"
        f"**Apontam para esta tabela:**\n\n{de_entrada}"
    )


def _secao_indices(r: dict[str, Any]) -> str:
    restricoes = sorted(
        (x for x in r.get("restricoes", []) if x["tipo"] in ORDEM_RESTRICAO),
        key=lambda x: (ORDEM_RESTRICAO[x["tipo"]], x["nome"]),
    )
    linhas = [
        [x["nome"], ROTULO_RESTRICAO[x["tipo"]],
         codigo_inline(x["definicao"]) if x.get("definicao") else ", ".join(x["colunas"])]
        for x in restricoes
    ]
    nomes_de_chave = {x["nome"] for x in restricoes if x["tipo"] in ("pk", "uk")}
    for i in sorted((i for i in r.get("indices", []) if i["nome"] not in nomes_de_chave), key=lambda i: i["nome"]):
        rotulo = ("índice único" if i["unico"] else "índice") + ("" if i["valido"] else " [inválido]")
        linhas.append([i["nome"], rotulo, codigo_inline(i["definicao"]) if i.get("definicao") else TRAVESSAO])
    corpo = tabela(["Nome", "Tipo", "Definição"], linhas) if linhas else NENHUM
    return f"## Índices e restrições\n\n{corpo}"


def _momento_e_evento(gatilho: dict[str, Any]) -> str:
    eventos = sorted(gatilho.get("eventos", []), key=lambda e: ORDEM_EVENTO.get(e, 9))
    texto = " ".join(x for x in (gatilho.get("momento"), " OR ".join(eventos)) if x) or TRAVESSAO
    nivel = {"ROW": "linha", "STATEMENT": "comando"}.get(gatilho.get("nivel") or "")
    if nivel:
        texto += f" ({nivel})"
    if gatilho.get("habilitado") == "desabilitado":
        texto += " [desabilitado]"
    return texto


def _secao_gatilhos(ctx: Contexto, chave: Chave, caminho: str) -> str:
    linhas = []
    for g in sorted(ctx.relacoes[chave].get("gatilhos", []), key=lambda g: g["nome"]):
        nome_funcao = f"{g['funcao_schema']}.{g['funcao_nome']}" if g.get("funcao_nome") else TRAVESSAO
        funcao = ctx.funcao_do_gatilho.get((chave, g["nome"]))
        linhas.append([g["nome"], _momento_e_evento(g), _link_funcao(ctx, caminho, funcao, nome_funcao)])
    corpo = tabela(["Gatilho", "Momento e evento", "Função de gatilho"], linhas) if linhas else NENHUM
    return f"## Rotinas e gatilhos\n\n{corpo}"


def _secao_dependencias(ctx: Contexto, chave: Chave, caminho: str) -> str:
    r = ctx.relacoes[chave]
    partes = ["## Dependências"]
    if r["tipo"] in ("view", "materializada"):
        deps = sorted({(d["schema"], d["relacao"], d.get("tipo") or TRAVESSAO) for d in r.get("depende_de", [])})
        linhas = [[_link_relacao(ctx, caminho, (s, n)), t] for s, n, t in deps]
        partes.append("### Depende de\n\n" + (tabela(["Objeto", "Tipo"], linhas) if linhas else NENHUM))
    dependentes = sorted(set(ctx.views_dependentes.get(chave, [])))
    linhas = [[_link_relacao(ctx, caminho, v), _tipo_texto(ctx.relacoes[v])] for v in dependentes]
    partes.append("### Views que dependem desta\n\n" + (tabela(["View", "Tipo"], linhas) if linhas else NENHUM))
    return "\n\n".join(partes)


def _secao_definicao(ctx: Contexto, chave: Chave) -> str:
    definicao = ctx.relacoes[chave].get("definicao")
    onde = f"`catalogo/estrutura/{chave[0]}.json` (relação `{chave[1]}`)"
    corpo = bloco_codigo_limitado(definicao, onde) if definicao else "Definição indisponível no catálogo."
    return f"## Definição\n\n{corpo}"


def _secao_particoes(ctx: Contexto, chave: Chave, caminho: str) -> str:
    linhas = [
        [
            _link_relacao(ctx, caminho, filha),
            linhas_arredondadas(_linhas_da_relacao(ctx, filha)),
            codigo_inline((ctx.relacoes[filha].get("particao") or {}).get("limite") or TRAVESSAO),
        ]
        for filha in sorted(ctx.filhas.get(chave, []))
    ]
    corpo = tabela(["Partição", "Linhas (est.)", "Limite"], linhas) if linhas else NENHUM
    return f"## Partições\n\n{corpo}"


# ----------------------------------------------------------------- ficha de rotina


def assinatura(funcao: dict[str, Any]) -> str:
    argumentos = funcao.get("argumentos")
    if argumentos is None:
        argumentos = funcao.get("argumentos_identidade") or ""
    return f"{funcao['nome']}({linha_unica(argumentos)})"


def _tipo_da_rotina(funcao: dict[str, Any]) -> str:
    if funcao.get("tipo") == "function" and funcao.get("retorno") in ("trigger", "event_trigger"):
        return "função de gatilho"
    return TIPOS_ROTINA.get(funcao.get("tipo") or "function", "function")


def ficha_rotina(ctx: Contexto, objeto: Objeto) -> list[Bloco]:
    f = ctx.funcoes[objeto]
    caminho = ctx.caminhos[objeto]
    return [
        _auto("cabecalho", _cabecalho_rotina(objeto, f)),
        pendente("Objetivo", 4),
        pendente("Parâmetros", 4),
        pendente("Retorno", 4),
        _auto("dependencias", _dependencias_rotina(ctx, objeto, caminho)),
        pendente("Regras de negócio", 4),
        pendente("Efeitos colaterais e cuidados", 4),
        _auto("codigo", _secao_codigo(objeto, f)),
        historico(ctx),
    ]


SECOES_ROTINA = ["cabecalho", "dependencias", "codigo"]


def _cabecalho_rotina(objeto: Objeto, f: dict[str, Any]) -> str:
    partes = [_tipo_da_rotina(f)]
    if f.get("linguagem"):
        partes.append(f"linguagem {f['linguagem']}")
    if f.get("retorno"):
        partes.append(f"retorna {linha_unica(f['retorno'])}")
    if f.get("volatilidade"):
        partes.append(VOLATILIDADE[f["volatilidade"]])
    if f.get("security_definer"):
        partes.append("SECURITY DEFINER")
    sem_execute = f.get("acesso") == "sem_execute"
    partes.append("acesso: sem_execute" if sem_execute else "acesso: com_execute")
    texto = f"# {objeto.schema}.{assinatura(f)}\n\n> " + " · ".join(partes)
    if sem_execute:
        texto += f"\n\n{FRASE_SEM_EXECUTE}"
    return texto + _comentario(f.get("comentario"))


def _dependencias_rotina(ctx: Contexto, objeto: Objeto, caminho: str) -> str:
    chamadas = ctx.gatilhos_da_funcao.get(objeto, [])
    if not chamadas:
        return "## Dependências\n\nLevantamento na fase 4."
    linhas = [
        [
            "é chamada por gatilho",
            f"`{g['nome']}` em {_link_relacao(ctx, caminho, tabela_)}",
            codigo_inline(g["definicao"]) if g.get("definicao") else TRAVESSAO,
        ]
        for tabela_, g in sorted(chamadas, key=lambda x: (x[0], x[1]["nome"]))
    ]
    return "## Dependências\n\n" + tabela(["Tipo", "Objeto", "Como"], linhas)


def _secao_codigo(objeto: Objeto, f: dict[str, Any]) -> str:
    codigo = f.get("codigo")
    onde = f"`catalogo/estrutura/{objeto.schema}.json` (rotina `{f['nome']}`)"
    if codigo:
        corpo = bloco_codigo_limitado(codigo, onde)
    elif f.get("tipo") == "aggregate":
        corpo = "Agregado: o catálogo não tem `pg_get_functiondef` para este tipo de objeto."
    else:
        corpo = "Código indisponível no catálogo."
    return f"## Código\n\n{corpo}"


# ----------------------------------------------------------------- README do schema

SECOES_README = ["cabecalho", "inventario", "rotinas", "sequencias", "tipos"]


def _contagens_do_schema(ctx: Contexto, schema: str) -> tuple[int, int, int, int]:
    relacoes = [r for (s, _), r in ctx.relacoes.items() if s == schema]
    views = sum(1 for r in relacoes if r["tipo"] in ("view", "materializada"))
    particoes = sum(1 for r in relacoes if r.get("particao"))
    rotinas = sum(1 for o in ctx.funcoes if o.schema == schema)
    return len(relacoes) - views, views, rotinas, particoes


def _tamanho_do_schema(ctx: Contexto, schema: str) -> int | None:
    tamanhos = [
        e["tamanho_total_bytes"] for (s, _), e in ctx.estatisticas.items()
        if s == schema and isinstance(e.get("tamanho_total_bytes"), int)
    ]
    return sum(tamanhos) if tamanhos else None


def resumo_do_schema(ctx: Contexto, schema: str) -> str:
    tabelas, views, rotinas, _ = _contagens_do_schema(ctx, schema)
    return f"{tabelas} tabelas · {views} views · {rotinas} rotinas"


def ficha_readme(ctx: Contexto, schema: str) -> list[Bloco]:
    return [
        _auto("cabecalho", _cabecalho_readme(ctx, schema)),
        pendente("Do que trata", 3),
        pendente("Por onde começar", 3),
        pendente("Grupos de tabelas", 2),
        _auto("inventario", _secao_inventario(ctx, schema)),
        _auto("rotinas", _secao_rotinas_readme(ctx, schema)),
        _auto("sequencias", _secao_sequencias(ctx, schema)),
        _auto("tipos", _secao_tipos(ctx, schema)),
        pendente("Ligações com outros schemas", 2),
    ]


def _cabecalho_readme(ctx: Contexto, schema: str) -> str:
    _, _, _, particoes = _contagens_do_schema(ctx, schema)
    partes = [resumo_do_schema(ctx, schema)]
    if particoes:
        partes.append(f"{particoes} partições (já contadas em tabelas)")
    partes.append(tamanho_legivel(_tamanho_do_schema(ctx, schema)))
    comentario = _comentario(ctx.estruturas[schema].get("comentario"))
    return f"# Schema {schema}\n\n> " + " · ".join(partes) + comentario


def _secao_inventario(ctx: Contexto, schema: str) -> str:
    readme = caminho_readme(schema)
    chaves = sorted(k for k in ctx.relacoes if k[0] == schema)
    linhas = []
    classes: dict[str, int] = {}
    for chave in chaves:
        r = ctx.relacoes[chave]
        item = ctx.triagem.get(chave)
        if item:
            classes[item["classe"]] = classes.get(item["classe"], 0) + 1
        linhas.append([
            chave[1], _tipo_texto(r), linhas_arredondadas(_linhas_da_relacao(ctx, chave)),
            item["classe"] if item else TRAVESSAO, link(readme, ctx.caminho_relacao[chave], "ficha"),
        ])
    if not linhas:
        return f"## Inventário\n\n{NENHUM}"
    resumo = " · ".join(f"{c} {n}" for c, n in sorted(classes.items())) or NENHUM.rstrip(".")
    corpo = tabela(["Objeto", "Tipo", "Linhas (est.)", "Triagem", "Ficha"], linhas)
    return f"## Inventário\n\nTriagem: {resumo}.\n\n{corpo}"


def _secao_rotinas_readme(ctx: Contexto, schema: str) -> str:
    readme = caminho_readme(schema)
    objetos = sorted((o for o in ctx.funcoes if o.schema == schema), key=lambda o: (o.nome, o.args or ""))
    linhas = [
        [assinatura(ctx.funcoes[o]), _tipo_da_rotina(ctx.funcoes[o]), ctx.funcoes[o].get("linguagem") or TRAVESSAO,
         link(readme, ctx.caminhos[o], "ficha")]
        for o in objetos
    ]
    corpo = tabela(["Rotina", "Tipo", "Linguagem", "Ficha"], linhas) if linhas else NENHUM
    return f"## Rotinas\n\n{corpo}"


def _secao_sequencias(ctx: Contexto, schema: str) -> str:
    sequencias = sorted(ctx.estruturas[schema].get("sequencias", []), key=lambda s: s["nome"])
    linhas = []
    for s in sequencias:
        dona = (
            f"{s['coluna_schema']}.{s['coluna_relacao']}.{s['coluna_nome']}"
            if s.get("coluna_relacao") and s.get("coluna_nome") else TRAVESSAO
        )
        linhas.append([s["nome"], s.get("tipo") or TRAVESSAO, dona, "sim" if s.get("por_identidade") else "não"])
    corpo = tabela(["Sequência", "Tipo", "Coluna dona", "Identidade"], linhas) if linhas else NENHUM
    return f"## Sequências\n\n{corpo}"


def _detalhe_do_tipo(t: dict[str, Any]) -> str:
    if t["categoria"] == "enum":
        return ", ".join(t.get("valores", [])) or TRAVESSAO
    if t["categoria"] == "composto":
        return ", ".join(t.get("atributos", [])) or TRAVESSAO
    return f"base: {t['tipo_base']}" if t.get("tipo_base") else TRAVESSAO


def _secao_tipos(ctx: Contexto, schema: str) -> str:
    tipos = sorted(ctx.estruturas[schema].get("tipos", []), key=lambda t: t["nome"])
    linhas = [[t["nome"], ESPECIES_TIPO[t["categoria"]], _detalhe_do_tipo(t)] for t in tipos]
    corpo = tabela(["Tipo", "Espécie", "Valores ou atributos"], linhas) if linhas else NENHUM
    return f"## Tipos\n\n{corpo}"


# ----------------------------------------------------------------- llms.txt

CAMINHOS_ESSENCIAIS = (
    ("glossario.md", "Glossário", "termos do negócio e onde estão no banco", False),
    ("receitas", "Receitas", "consultas prontas e validadas", True),
    ("duvidas.md", "Dúvidas em aberto", None, False),
)


def _fatos_essenciais(ctx: Contexto) -> list[str]:
    tenant = ctx.fatos.get("tenant")
    linhas: list[str] = []
    if isinstance(tenant, dict):
        if tenant.get("descricao"):
            linhas.append(f"- {linha_unica(str(tenant['descricao']))}")
        if tenant.get("coluna"):
            linhas.append(f"- Coluna de tenant: `{tenant['coluna']}`.")
        if tenant.get("regra"):
            linhas.append(f"- {linha_unica(str(tenant['regra']))}")
    if (ctx.doc / "config" / "fatos.yml").is_file():
        linhas.append("- Demais fatos e valores: `config/fatos.yml`.")
    return linhas or [f"- {NENHUM}"]


def _comece_por_aqui(ctx: Contexto) -> list[str]:
    linhas = []
    for caminho, titulo, descricao, pasta in CAMINHOS_ESSENCIAIS:
        existe = (ctx.doc / caminho).is_dir() if pasta else (ctx.doc / caminho).is_file()
        if existe:
            alvo = f"{caminho}/" if pasta else caminho
            linhas.append(f"- [{titulo}]({alvo})" + (f": {descricao}" if descricao else ""))
    return linhas or [f"- {NENHUM}"]


def conteudo_llms(ctx: Contexto) -> str:
    descricao = (ctx.projeto_descricao.strip() + " " if ctx.projeto_descricao.strip() else "")
    schemas = [
        f"- [{s}]({caminho_readme(s)}): {resumo_do_schema(ctx, s)}" for s in ctx.schemas
    ]
    return "\n".join([
        f"# {ctx.projeto_nome}",
        "",
        f"> {descricao}{AVISO_ESQUELETO}",
        "",
        PARAGRAFO_CONFIANCA,
        "",
        "## Fatos essenciais",
        "",
        *_fatos_essenciais(ctx),
        "",
        "## Comece por aqui",
        "",
        *_comece_por_aqui(ctx),
        "",
        "## Schemas",
        "",
        *(schemas or [f"- {NENHUM}"]),
    ])
