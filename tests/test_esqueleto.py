"""Testes offline do esqueleto (tarefa 1.4, D-013). Catálogo sintético, nomes inventados."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from pg_atlas import cli, esqueleto, triagem
from pg_atlas.esqueleto_formato import (
    bloco_codigo,
    bloco_codigo_limitado,
    celula,
    distintos,
    linhas_arredondadas,
    tamanho_legivel,
)
from pg_atlas.esqueleto_marcadores import ErroMarcadores, atualizar, interpretar, montar_novo, neutralizar
from pg_atlas.esqueleto_nomes import ErroEsqueleto, Objeto, atribuir_caminhos, nome_base

BASE = "2026-03-04T05:06:07+00:00"
RAIZ_TEMPLATES = Path(__file__).resolve().parents[1] / "templates"

# ----------------------------------------------------------------- nomes de arquivo


@pytest.mark.parametrize(
    "nome,esperado",
    [
        ("Tabela_X", "tabela_x"),
        ("proc_a_[mascarado:cnpj:12]", "proc_a_mascarado-cnpj-12"),
        ("[mascarado:email]", "mascarado-email"),
        ("a.b c", "a~2Eb~20c"),
        ("a~b", "a~7Eb"),
        ("pt-br$x", "pt-br$x"),
        ("ação", "ação"),
        ("a/b|c", "a~2Fb~7Cc"),
        ("CON", "con~"),
        ("lpt3", "lpt3~"),
        ("console", "console"),
    ],
)
def test_nome_base(nome: str, esperado: str) -> None:
    assert nome_base(nome) == esperado


def test_nome_longo_vira_corte_e_hash() -> None:
    base = nome_base("x" * 300)
    assert len(base) == 109 and base.startswith("x" * 100 + "~")
    assert nome_base("x" * 300) == base
    assert nome_base("x" * 299) != base


def test_conversao_nao_gera_colisao_entre_pontuacao_e_caractere_literal() -> None:
    assert nome_base("a.b") != nome_base("a~2Eb".replace("~2E", "_"))
    assert nome_base("a.b") != nome_base("a~2eb")


def obj(nome: str, args: str | None = None, schema: str = "s", pasta: str = "funcoes", tipo: str = "function") -> Objeto:
    return Objeto(schema, pasta, tipo, nome, args)


def test_sem_colisao_nao_leva_sufixo() -> None:
    caminhos = atribuir_caminhos([obj("f", "integer")])
    assert list(caminhos.values()) == ["schemas/s/funcoes/f.md"]


def test_sobrecarga_e_caixa_formam_grupo_com_sufixo_ordenado() -> None:
    a, b, c = obj("f", "text"), obj("f", "integer"), obj("F", "")
    caminhos = atribuir_caminhos([a, b, c])
    # ordem: nome original por ponto de código ('F' < 'f'), depois argumentos
    assert caminhos[c] == "schemas/s/funcoes/f__1.md"
    assert caminhos[b] == "schemas/s/funcoes/f__2.md"
    assert caminhos[a] == "schemas/s/funcoes/f__3.md"


def test_colisao_residual_falha_sem_gerar_nada() -> None:
    with pytest.raises(ErroEsqueleto, match="Colisão"):
        atribuir_caminhos([obj("f", "a"), obj("f", "b"), obj("f__2", "c")])


def test_pastas_diferentes_nao_colidem() -> None:
    caminhos = atribuir_caminhos([obj("x", pasta="tabelas", tipo="tabela"), obj("x", "")])
    assert len(set(caminhos.values())) == 2


def test_schemas_com_a_mesma_pasta_falham() -> None:
    with pytest.raises(ErroEsqueleto, match="pasta"):
        atribuir_caminhos([obj("x", schema="Ab"), obj("y", schema="aB")])


# ----------------------------------------------------------------- formatação


@pytest.mark.parametrize(
    "n,esperado",
    [(None, "—"), (0, "0"), (37, "37"), (99, "99"), (123, "120"), (1234567, "1.200.000"), (15500, "16.000")],
)
def test_linhas_com_dois_algarismos_significativos(n: int | None, esperado: str) -> None:
    assert linhas_arredondadas(n) == esperado


@pytest.mark.parametrize(
    "n,esperado",
    [(0, "0 B"), (1023, "1023 B"), (1024, "1,0 KB"), (1536, "1,5 KB"), (5 * 1024**3, "5,0 GB"), (None, "—")],
)
def test_tamanho_legivel(n: int | None, esperado: str) -> None:
    assert tamanho_legivel(n) == esperado


def test_distintos_positivo_inteiro_negativo_fracao() -> None:
    assert distintos(1200) == "1.200"
    assert distintos(-1) == "≈100% das linhas"
    assert distintos(-0.333) == "≈33,3% das linhas"
    assert distintos(None) == "—"


def test_celula_escapa_pipe_e_quebra_de_linha() -> None:
    assert celula("a|b\nc\r\nd") == "a\\|b c d"


def test_cerca_tem_mais_crases_que_o_conteudo() -> None:
    assert bloco_codigo("x").startswith("```sql\n")
    assert bloco_codigo("a ``` b").startswith("````sql\n")
    assert bloco_codigo("a\r\nb\n").endswith("a\nb\n```")


def test_bloco_acima_de_2mb_vira_aviso() -> None:
    texto = "linha\n" * (2 * 1024 * 1024 // 6 + 10)
    saida = bloco_codigo_limitado(texto, "`catalogo/estrutura/s.json`")
    assert "omitido" in saida and "catalogo/estrutura/s.json" in saida and "linha\n" not in saida


# ----------------------------------------------------------------- marcadores


def arquivo_com(*secoes: tuple[str, str]) -> str:
    """Arquivo com texto humano entre as seções automáticas."""
    partes = ["# Título humano", "Texto humano 1."]
    for i, (nome, conteudo) in enumerate(secoes):
        partes += [f'<!-- pg-atlas:auto inicio secao="{nome}" -->', conteudo, "<!-- pg-atlas:auto fim -->"]
        partes.append(f"Texto humano depois de {nome} ({i}).")
    return "\n".join(partes) + "\n"


def test_atualizar_so_reescreve_dentro_dos_marcadores() -> None:
    antigo = arquivo_com(("a", "velho A"), ("b", "velho B"))
    novo, avisos = atualizar(antigo, [("a", "novo A"), ("b", "novo B")])
    assert novo == antigo.replace("velho A", "novo A").replace("velho B", "novo B")
    assert avisos == []


def test_atualizar_e_idempotente() -> None:
    antigo = arquivo_com(("a", "A"), ("b", "B"))
    uma, _ = atualizar(antigo, [("a", "A2"), ("b", "B2")])
    duas, _ = atualizar(uma, [("a", "A2"), ("b", "B2")])
    assert uma == duas


def test_secao_ausente_entra_logo_apos_a_anterior_mais_proxima() -> None:
    antigo = arquivo_com(("a", "A"), ("c", "C"))
    novo, _ = atualizar(antigo, [("a", "A"), ("b", "B"), ("c", "C")])
    linhas = novo.split("\n")
    posicoes = interpretar(linhas)
    assert posicoes["a"][1] < posicoes["b"][0] < posicoes["c"][0]
    assert linhas[posicoes["a"][1] + 1] == ""  # logo após o fim de "a"
    assert "Texto humano depois de a (0)." in novo  # texto humano preservado
    assert linhas.index("Texto humano depois de a (0).") > posicoes["b"][1]


def test_secao_ausente_sem_anterior_entra_antes_da_proxima() -> None:
    antigo = arquivo_com(("b", "B"))
    novo, _ = atualizar(antigo, [("a", "A"), ("b", "B")])
    posicoes = interpretar(novo.split("\n"))
    assert posicoes["a"][1] < posicoes["b"][0]
    assert novo.startswith("# Título humano\nTexto humano 1.\n<!-- pg-atlas:auto inicio secao=\"a\"")


def test_secao_ausente_sem_nenhuma_existente_vai_para_topo_ou_fim() -> None:
    topo, _ = atualizar("texto humano\n", [("cabecalho", "C")])
    assert topo.startswith("<!-- pg-atlas:auto inicio secao=\"cabecalho\" -->\nC\n<!-- pg-atlas:auto fim -->\n")
    assert topo.endswith("texto humano\n")
    fim, _ = atualizar("texto humano\n", [("x", "X"), ("y", "Y")])
    assert fim.startswith("texto humano\n") and 'secao="y"' in fim and fim.endswith("<!-- pg-atlas:auto fim -->\n")
    vazio, _ = atualizar("", [("x", "X")])
    assert vazio == montar_novo([("x", "X")])


def test_secao_obsoleta_fica_intacta_com_aviso() -> None:
    antigo = arquivo_com(("a", "A"), ("velha", "V"))
    novo, avisos = atualizar(antigo, [("a", "A2")])
    assert "V" in novo and 'secao="velha"' in novo
    assert len(avisos) == 1 and "velha" in avisos[0]


@pytest.mark.parametrize(
    "texto",
    [
        '<!-- pg-atlas:auto inicio secao="a" -->\nx\n',  # início sem fim
        "x\n<!-- pg-atlas:auto fim -->\n",  # fim sem início
        '<!-- pg-atlas:auto inicio secao="a" -->\n<!-- pg-atlas:auto inicio secao="b" -->\n'
        "<!-- pg-atlas:auto fim -->\n<!-- pg-atlas:auto fim -->\n",  # aninhamento
        '<!-- pg-atlas:auto inicio secao="a" -->\n<!-- pg-atlas:auto fim -->\n'
        '<!-- pg-atlas:auto inicio secao="a" -->\n<!-- pg-atlas:auto fim -->\n',  # repetida
        '<!-- pg-atlas:auto inicio secao="" -->\n<!-- pg-atlas:auto fim -->\n',  # sem nome
        '<!-- pg-atlas:auto inicio secao="Maiúscula" -->\n<!-- pg-atlas:auto fim -->\n',  # nome inválido
        '<!-- pg-atlas:auto inicio -->\n<!-- pg-atlas:auto fim -->\n',  # mal formado
        'texto <!-- pg-atlas:auto fim -->\n',  # não está sozinho na linha
    ],
)
def test_marcadores_quebrados_geram_erro(texto: str) -> None:
    with pytest.raises(ErroMarcadores):
        atualizar(texto, [("a", "A")])


def test_neutralizar_impede_marcador_falso_no_conteudo() -> None:
    perigoso = "x <!-- pg-atlas:auto fim --> y <!-- pg-atlas:pendente fase=\"3\" -->"
    seguro = neutralizar(perigoso)
    assert interpretar(montar_novo([("s", seguro)]).split("\n")) is not None
    assert "pg-atlas:pendente" not in seguro and "pg-atlas:auto fim" not in seguro


# ----------------------------------------------------------------- catálogo sintético


def col(ordem: int, nome: str, tipo: str = "integer", obrigatoria: bool = False, padrao: str | None = None) -> dict:
    return {
        "ordem": ordem, "nome": nome, "tipo": tipo, "obrigatoria": obrigatoria, "padrao": padrao,
        "identidade": None, "gerada": None, "expressao_gerada": None, "comentario": None,
    }


def restr(tipo: str, nome: str, colunas: list[str], definicao: str | None = None, **kw: Any) -> dict:
    base = {
        "tipo": tipo, "nome": nome, "colunas": colunas, "definicao": definicao, "herdada": False,
        "ref_schema": None, "ref_relacao": None, "ref_colunas": [],
    }
    return base | kw


def indice(nome: str, unico: bool = False) -> dict:
    return {"nome": nome, "unico": unico, "valido": True, "definicao": f"CREATE INDEX {nome} ON x (a)"}


def rel(nome: str, tipo: str = "tabela", **kw: Any) -> dict:
    base = {
        "nome": nome, "tipo": tipo, "acesso": "ok", "particao": None, "chave_particao": None,
        "comentario": None, "definicao": None, "colunas": [col(1, "id", obrigatoria=True)],
        "restricoes": [], "indices": [], "gatilhos": [], "depende_de": [],
    }
    return base | kw


def fn(nome: str, args: str = "", ident: str | None = None, **kw: Any) -> dict:
    base = {
        "nome": nome, "tipo": "function", "argumentos": args, "argumentos_identidade": args if ident is None else ident,
        "retorno": "integer", "linguagem": "sql", "volatilidade": "volatil", "security_definer": False,
        "acesso": "ok", "codigo": f"CREATE FUNCTION {nome}() ...", "comentario": None,
    }
    return base | kw


def est(nome: str, linhas: int | None = 10, tamanho: int | None = 8192, tipo: str = "tabela") -> dict:
    return {
        "nome": nome, "tipo": tipo, "linhas_estimadas": linhas, "linhas_vivas": linhas,
        "tamanho_total_bytes": tamanho, "seq_scans": 123456789, "ultimo_analyze": "2026-01-01T00:00:00",
        "colunas": [{"nome": "id", "null_frac": 0.0, "n_distinct": -1.0, "herdada": False}],
    }


def montar_catalogo(tmp_path: Path) -> Path:
    doc = tmp_path / "doc"
    (doc / "config").mkdir(parents=True)
    (doc / "config" / "projeto.yml").write_text(
        'projeto:\n  nome: "Projeto sintético"\n  descricao: "Banco inventado."\n', encoding="utf-8"
    )
    (doc / "config" / "fatos.yml").write_text(
        'tenant:\n  descricao: "Banco multi-tenant."\n  coluna: id_cliente\n  regra: "Filtrar por tenant."\n',
        encoding="utf-8",
    )
    (doc / "_controle").mkdir()
    relacoes_s1 = [
        rel("Pai", restricoes=[restr("pk", "pk_pai", ["id"], "PRIMARY KEY (id)")], indices=[indice("pk_pai", True)],
            gatilhos=[{"nome": "trg_x", "eventos": ["UPDATE", "INSERT"], "momento": "BEFORE", "nivel": "ROW",
                       "habilitado": "origem", "funcao_schema": "s1", "funcao_nome": "func_trg",
                       "definicao": "CREATE TRIGGER trg_x ..."}]),
        rel("filha", colunas=[col(1, "id", obrigatoria=True), col(2, "id_pai", padrao="0")],
            restricoes=[restr("fk", "fk_filha_pai", ["id_pai"], ref_schema="s1", ref_relacao="Pai",
                              ref_colunas=["id"])]),
        rel("abc"), rel("ABC"),
        rel("CON"),
        rel("proc_[mascarado:cnpj:1]"),
        rel("secreta", acesso="sem_select"),
        rel("hist", "particionada", chave_particao="LIST (id_cliente)"),
        rel("hist_p1", particao={"pai_schema": "s1", "pai_nome": "hist", "limite": "FOR VALUES IN ('a')"}),
        rel("vw_pai", "view", definicao=" SELECT 1;", depende_de=[{"schema": "s1", "relacao": "Pai", "tipo": "tabela"}]),
        rel("mv", "materializada", definicao="SELECT 1", indices=[indice("ix_mv")]),
    ]
    funcoes_s1 = [
        fn("func_trg", retorno="trigger", linguagem="plpgsql"),
        fn("sobrecarga", "a integer", "integer"),
        fn("sobrecarga", "a text", "text"),
        fn("agg", tipo="aggregate", codigo=None),
        fn("```cerca", codigo="SELECT '```';"),
        fn("fechada", acesso="sem_execute", security_definer=True, volatilidade="imutavel"),
    ]
    estruturas = {
        "s1": {"schema": "s1", "relacoes": relacoes_s1, "funcoes": funcoes_s1, "comentario": "Schema um.",
               "sequencias": [{"nome": "seq", "tipo": "bigint", "coluna_schema": "s1", "coluna_relacao": "Pai",
                               "coluna_nome": "id", "por_identidade": False}],
               "tipos": [{"nome": "cor", "categoria": "enum", "valores": ["a", "b"], "atributos": [], "tipo_base": None}]},
        "s2": {"schema": "s2", "relacoes": [], "funcoes": [], "comentario": None, "sequencias": [], "tipos": []},
    }
    estatisticas = {
        "s1": [est(r["nome"], tipo=r["tipo"]) for r in relacoes_s1 if r["nome"] != "hist"]
        + [est("hist", 0, 0, "particionada")],
        "s2": [],
    }
    estatisticas["s1"][[e["nome"] for e in estatisticas["s1"]].index("hist_p1")] = est("hist_p1", 3_456_789, 2 * 1024**3)
    cat = doc / "catalogo"
    for pasta in ("estrutura", "estatisticas"):
        (cat / pasta).mkdir(parents=True)
    for schema, estrutura in estruturas.items():
        (cat / "estrutura" / f"{schema}.json").write_text(json.dumps(estrutura), encoding="utf-8")
        (cat / "estatisticas" / f"{schema}.json").write_text(
            json.dumps({"formato": "estatisticas", "schema": schema, "relacoes": estatisticas[schema]}),
            encoding="utf-8",
        )
    (cat / "_banco.json").write_text(
        json.dumps({"formato": "banco", "extraido_em": BASE, "schemas": {s: {} for s in estruturas}}),
        encoding="utf-8",
    )
    triagem.executar_triagem(doc, doc / "_controle")
    return doc


@pytest.fixture
def doc(tmp_path: Path) -> Path:
    return montar_catalogo(tmp_path)


def gerar(doc: Path) -> esqueleto.Resultado:
    return esqueleto.executar_esqueleto(doc, doc / "_controle", "Projeto sintético", "Banco inventado.")


def arquivos(doc: Path) -> dict[str, bytes]:
    return {
        p.relative_to(doc).as_posix(): p.read_bytes()
        for pasta in ("schemas",)
        for p in sorted((doc / pasta).rglob("*")) if p.is_file()
    } | {"llms.txt": (doc / "llms.txt").read_bytes(), "catalogo/_fichas.json": (doc / "catalogo/_fichas.json").read_bytes()}


def mapa(doc: Path) -> list[dict]:
    return json.loads((doc / "catalogo" / "_fichas.json").read_text(encoding="utf-8"))["fichas"]


# ----------------------------------------------------------------- critério 1: uma ficha por objeto


def test_uma_ficha_por_relacao_e_por_rotina_sem_colisao(doc: Path) -> None:
    resultado = gerar(doc)
    fichas = mapa(doc)
    caminhos = [f["caminho"] for f in fichas]
    assert len(fichas) == 11 + 6
    assert len({c.casefold() for c in caminhos}) == len(caminhos)
    assert all((doc / c).is_file() for c in caminhos)
    assert resultado.por_tipo == {
        "tabela": 7, "particionada": 1, "particao": 1, "view": 1, "materializada": 1,
        "function": 5, "aggregate": 1,
    }
    assert (doc / "schemas/s1/README.md").is_file() and (doc / "schemas/s2/README.md").is_file()
    chaves = {(f["schema"], f["tipo"], f["nome"], f["argumentos_identidade"]) for f in fichas}
    assert ("s1", "tabela", "Pai", None) in chaves
    assert ("s1", "function", "sobrecarga", "text") in chaves


def test_nomes_de_arquivo_das_fichas(doc: Path) -> None:
    gerar(doc)
    por_nome = {(f["nome"], f["argumentos_identidade"]): f["caminho"] for f in mapa(doc)}
    assert por_nome[("ABC", None)] == "schemas/s1/tabelas/abc__1.md"  # 'A' < 'a' por ponto de código
    assert por_nome[("abc", None)] == "schemas/s1/tabelas/abc__2.md"
    assert por_nome[("CON", None)] == "schemas/s1/tabelas/con~.md"
    assert por_nome[("proc_[mascarado:cnpj:1]", None)] == "schemas/s1/tabelas/proc_mascarado-cnpj-1.md"
    assert por_nome[("sobrecarga", "integer")] == "schemas/s1/funcoes/sobrecarga__1.md"
    assert por_nome[("sobrecarga", "text")] == "schemas/s1/funcoes/sobrecarga__2.md"
    assert por_nome[("```cerca", "")] == "schemas/s1/funcoes/~60~60~60cerca.md"


def test_ficha_de_tabela_tem_secoes_na_ordem_e_pendencias(doc: Path) -> None:
    gerar(doc)
    texto = (doc / "schemas/s1/tabelas/pai.md").read_text(encoding="utf-8")
    ordem = re.findall(r'secao="([a-z_]+)"|^## (.+)(?=\n\n<!-- pg-atlas:pendente)|^## (Histórico)$', texto, re.M)
    titulos = [a or b or c for a, b, c in ordem]
    assert titulos == [
        "cabecalho", "Propósito", "Como usar", "colunas", "Significado das colunas", "relacionamentos",
        "indices", "rotinas", "dependencias", "Consultas típicas", "Histórico",
    ]
    assert texto.count('<!-- pg-atlas:pendente fase="3" -->\n_Pendente: a preencher na fase 3._') == 4
    assert "- 2026-03-04: ficha criada pela fase 1." in texto
    assert "[s1.filha](filha.md) | id_pai | declarado" in texto  # FK de entrada
    assert "[s1.vw_pai](vw_pai.md) | view" in texto  # view dependente
    assert "BEFORE INSERT OR UPDATE (linha) | [s1.func_trg](../funcoes/func_trg.md)" in texto
    assert "# s1.Pai\n\n> tabela · 10 linhas (estimativa) · 8,0 KB · triagem: " in texto


def test_ficha_de_view_tem_definicao_e_particionada_tem_particoes(doc: Path) -> None:
    gerar(doc)
    view = (doc / "schemas/s1/tabelas/vw_pai.md").read_text(encoding="utf-8")
    assert 'secao="definicao"' in view and "```sql\n SELECT 1;\n```" in view
    assert "### Depende de" in view and "[s1.Pai](pai.md) | tabela" in view
    assert "triagem: não se aplica" in view
    pai = (doc / "schemas/s1/tabelas/hist.md").read_text(encoding="utf-8")
    assert 'secao="particoes"' in pai and "[s1.hist_p1](hist_p1.md) | 3.500.000 |" in pai
    assert "tabela particionada · 3.500.000 linhas" in pai and "2,0 GB" in pai  # soma das partições
    assert "Chave de partição: `LIST (id_cliente)`" in pai


def test_ficha_de_particao_e_resumida(doc: Path) -> None:
    gerar(doc)
    texto = (doc / "schemas/s1/tabelas/hist_p1.md").read_text(encoding="utf-8")
    assert "partição de [s1.hist](hist.md)" in texto
    assert "Limite da partição: `FOR VALUES IN ('a')`" in texto
    assert "Colunas e significado: ver a tabela-mãe." in texto
    assert 'secao="colunas"' not in texto and "pg-atlas:pendente" not in texto
    assert "## Histórico" in texto


def test_sem_select_e_sem_execute(doc: Path) -> None:
    gerar(doc)
    tabela = (doc / "schemas/s1/tabelas/secreta.md").read_text(encoding="utf-8")
    assert "acesso: sem_select" in tabela and "Sem permissão de SELECT para a credencial" in tabela
    rotina = (doc / "schemas/s1/funcoes/fechada.md").read_text(encoding="utf-8")
    assert "SECURITY DEFINER" in rotina and "imutável" in rotina and "acesso: sem_execute" in rotina


def test_ficha_de_rotina(doc: Path) -> None:
    gerar(doc)
    gatilho = (doc / "schemas/s1/funcoes/func_trg.md").read_text(encoding="utf-8")
    assert "> função de gatilho · linguagem plpgsql · retorna trigger" in gatilho
    assert "`trg_x` em [s1.Pai](../tabelas/pai.md)" in gatilho
    comum = (doc / "schemas/s1/funcoes/sobrecarga__1.md").read_text(encoding="utf-8")
    assert "# s1.sobrecarga(a integer)" in comum and "Levantamento na fase 4." in comum
    assert comum.count('fase="4"') == 5 and comum.index("secao=\"codigo\"") > comum.index("Efeitos colaterais")
    agregado = (doc / "schemas/s1/funcoes/agg.md").read_text(encoding="utf-8")
    assert "> agregado ·" in agregado and "pg_get_functiondef" in agregado
    cerca = (doc / "schemas/s1/funcoes/~60~60~60cerca.md").read_text(encoding="utf-8")
    assert "````sql\nSELECT '```';\n````" in cerca


def test_readme_do_schema(doc: Path) -> None:
    gerar(doc)
    texto = (doc / "schemas/s1/README.md").read_text(encoding="utf-8")
    assert "> 9 tabelas · 2 views · 6 rotinas · " in texto
    assert "1 partições (já contadas em tabelas)" in texto
    assert "| Pai | tabela |" in texto and "[ficha](tabelas/pai.md)" in texto
    assert "| hist_p1 | partição |" in texto
    assert "| cor | enum | a, b |" in texto and "| seq | bigint | s1.Pai.id | não |" in texto
    assert texto.count("pg-atlas:pendente") == 4
    vazio = (doc / "schemas/s2/README.md").read_text(encoding="utf-8")
    assert "0 tabelas · 0 views · 0 rotinas" in vazio and vazio.count("Nenhum.") == 4


def test_llms_provisorio(doc: Path) -> None:
    gerar(doc)
    texto = (doc / "llms.txt").read_text(encoding="utf-8")
    assert texto.startswith('<!-- pg-atlas:auto inicio secao="llms" -->\n# Projeto sintético\n')
    assert "> Banco inventado. Documentação em construção: esqueleto da fase 1, fichas só com estrutura." in texto
    assert "- Coluna de tenant: `id_cliente`." in texto and "- Demais fatos e valores: `config/fatos.yml`." in texto
    assert "- [s1](schemas/s1/README.md): 9 tabelas · 2 views · 6 rotinas" in texto
    assert "- [s2](schemas/s2/README.md): 0 tabelas · 0 views · 0 rotinas" in texto
    assert texto.index("- [s1]") < texto.index("- [s2]")
    (doc / "glossario.md").write_text("g", encoding="utf-8")
    gerar(doc)
    assert "- [Glossário](glossario.md)" in (doc / "llms.txt").read_text(encoding="utf-8")


# ----------------------------------------------------------------- critério 2: determinismo


def test_duas_execucoes_geram_arquivos_identicos(doc: Path) -> None:
    primeira = gerar(doc)
    antes = arquivos(doc)
    segunda = gerar(doc)
    assert arquivos(doc) == antes
    assert primeira.criados == len(antes) - 1  # tudo, menos _fichas.json
    assert segunda.criados == 0 and segunda.atualizados == 0 and segunda.inalterados == primeira.criados


def test_saida_em_utf8_lf_sem_bom_e_sem_data_de_execucao(doc: Path) -> None:
    gerar(doc)
    for caminho, dados in arquivos(doc).items():
        assert not dados.startswith(b"\xef\xbb\xbf"), caminho
        assert b"\r" not in dados and dados.endswith(b"\n"), caminho
        texto = dados.decode("utf-8")
        assert not re.search(r"\d{4}-\d{2}-\d{2}T\d", texto) or caminho.endswith(".json"), caminho
    assert json.loads((doc / "catalogo/_fichas.json").read_text(encoding="utf-8"))["fichas"] == mapa(doc)
    assert mapa(doc) == sorted(
        mapa(doc), key=lambda i: (i["schema"], i["tipo"], i["nome"], i["argumentos_identidade"] or "", i["caminho"])
    )


# ----------------------------------------------------------------- critério 3: regeneração


def test_regeneracao_preserva_texto_humano_e_reescreve_o_automatico(doc: Path) -> None:
    gerar(doc)
    ficha = doc / "schemas/s1/tabelas/pai.md"
    texto = ficha.read_text(encoding="utf-8")
    humano = texto.replace(
        '<!-- pg-atlas:pendente fase="3" -->\n_Pendente: a preencher na fase 3._',
        "Texto escrito pela fase 3 ÚNICO.", 1,
    )
    humano = humano.replace("| id | `integer` |", "| id | ALTERADO NA MÃO |") + "\nRodapé humano.\n"
    ficha.write_text(humano, encoding="utf-8")
    resultado = gerar(doc)
    novo = ficha.read_text(encoding="utf-8")
    assert "Texto escrito pela fase 3 ÚNICO." in novo and "Rodapé humano." in novo
    assert "ALTERADO NA MÃO" not in novo and "| 1 | id | `integer` |" in novo
    assert novo == texto.replace(
        '<!-- pg-atlas:pendente fase="3" -->\n_Pendente: a preencher na fase 3._',
        "Texto escrito pela fase 3 ÚNICO.", 1,
    ) + "\nRodapé humano.\n"
    assert resultado.atualizados == 1 and resultado.erros == []


def test_secao_ausente_e_reinserida_na_posicao_canonica(doc: Path) -> None:
    gerar(doc)
    ficha = doc / "schemas/s1/tabelas/pai.md"
    original = ficha.read_text(encoding="utf-8")
    sem_indices = re.sub(
        r'<!-- pg-atlas:auto inicio secao="indices" -->.*?<!-- pg-atlas:auto fim -->\n\n', "", original, flags=re.S
    )
    assert sem_indices != original
    ficha.write_text(sem_indices, encoding="utf-8")
    gerar(doc)
    assert ficha.read_text(encoding="utf-8") == original


def test_marcadores_quebrados_nao_sao_tocados_e_geram_erro(doc: Path) -> None:
    gerar(doc)
    quebrada = doc / "schemas/s1/tabelas/filha.md"
    outra = doc / "schemas/s1/tabelas/pai.md"
    quebrado = quebrada.read_text(encoding="utf-8").replace("<!-- pg-atlas:auto fim -->", "", 1)
    quebrada.write_text(quebrado, encoding="utf-8")
    outra.write_text(outra.read_text(encoding="utf-8").replace("Nenhum.", "Mudou.", 1), encoding="utf-8")
    resultado = gerar(doc)
    assert quebrada.read_text(encoding="utf-8") == quebrado
    assert len(resultado.erros) == 1 and "filha.md" in resultado.erros[0]
    assert "Mudou." not in outra.read_text(encoding="utf-8")  # as outras fichas foram regeneradas
    estado = json.loads((doc / "_controle/estado.json").read_text(encoding="utf-8"))
    assert estado["fases"]["1"]["tarefas"]["1.4"]["status"] == "com_erros"


def test_cli_retorna_erro_com_marcadores_quebrados(doc: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["esqueleto", "--doc", str(doc)]) == 0
    ficha = doc / "schemas/s1/tabelas/abc__1.md"
    ficha.write_text("<!-- pg-atlas:auto fim -->\n", encoding="utf-8")
    assert cli.main(["esqueleto", "--doc", str(doc)]) == 4
    assert "abc__1.md" in capsys.readouterr().err
    assert ficha.read_text(encoding="utf-8") == "<!-- pg-atlas:auto fim -->\n"


def test_ficha_orfa_e_listada_e_nunca_apagada(doc: Path) -> None:
    gerar(doc)
    orfa = doc / "schemas/s1/tabelas/removida.md"
    orfa.write_text("texto humano\n", encoding="utf-8")
    resultado = gerar(doc)
    assert resultado.orfas == ["schemas/s1/tabelas/removida.md"]
    assert orfa.read_text(encoding="utf-8") == "texto humano\n"
    registro = json.loads((doc / "_controle/esqueleto-orfas.json").read_text(encoding="utf-8"))
    assert registro["orfas"] == ["schemas/s1/tabelas/removida.md"]
    orfa.unlink()
    assert gerar(doc).orfas == []


def test_texto_fora_dos_marcadores_do_llms_e_preservado(doc: Path) -> None:
    gerar(doc)
    llms = doc / "llms.txt"
    llms.write_text(llms.read_text(encoding="utf-8") + "\nNota humana.\n", encoding="utf-8")
    gerar(doc)
    assert llms.read_text(encoding="utf-8").endswith("\nNota humana.\n")


# ----------------------------------------------------------------- critério 4: sem dados


def test_estatisticas_volateis_e_valores_nao_entram_nas_fichas(doc: Path) -> None:
    gerar(doc)
    todo = b"".join(v for k, v in arquivos(doc).items() if k.startswith("schemas/")).decode("utf-8")
    assert "123456789" not in todo  # contagem de scans
    assert "2026-01-01T00" not in todo  # último analyze
    for proibido in ("most_common_vals", "histogram_bounds"):
        assert proibido not in todo


def test_falha_sem_triagem_ou_catalogo(tmp_path: Path) -> None:
    doc = montar_catalogo(tmp_path)
    (doc / "catalogo" / "_triagem.json").unlink()
    with pytest.raises(ErroEsqueleto, match="triagem"):
        gerar(doc)
    (doc / "catalogo" / "estrutura" / "s2.json").unlink()
    (doc / "catalogo" / "_triagem.json").write_text("{}", encoding="utf-8")
    with pytest.raises(triagem.ErroTriagem, match="catalogo"):
        gerar(doc)


def test_colisao_residual_nao_gera_nada(tmp_path: Path) -> None:
    doc = montar_catalogo(tmp_path)
    caminho = doc / "catalogo" / "estrutura" / "s1.json"
    estrutura = json.loads(caminho.read_text(encoding="utf-8"))
    estrutura["relacoes"] += [rel("abc__2")]
    caminho.write_text(json.dumps(estrutura), encoding="utf-8")
    with pytest.raises(ErroEsqueleto, match="Colisão"):
        gerar(doc)
    assert not (doc / "schemas").exists() and not (doc / "llms.txt").exists()


def test_estado_json_registra_a_tarefa(doc: Path) -> None:
    gerar(doc)
    bloco = json.loads((doc / "_controle/estado.json").read_text(encoding="utf-8"))["fases"]["1"]["tarefas"]["1.4"]
    assert bloco["status"] == "concluida" and bloco["readmes"] == 2
    assert bloco["fichas_por_tipo"]["function"] == 5
    assert "1.3" in json.loads((doc / "_controle/estado.json").read_text(encoding="utf-8"))["fases"]["1"]["tarefas"]


# ----------------------------------------------------------------- modelos genéricos


def nomes_de_secao(arquivo: str) -> set[str]:
    return set(re.findall(r'secao="([a-z0-9_]+)"', (RAIZ_TEMPLATES / arquivo).read_text(encoding="utf-8")))


def test_modelos_declaram_as_secoes_geradas() -> None:
    from pg_atlas.esqueleto_fichas import SECOES_README, SECOES_ROTINA

    assert nomes_de_secao("ficha-tabela.md") == {
        "cabecalho", "colunas", "relacionamentos", "indices", "rotinas", "dependencias", "definicao", "particoes"
    }
    assert nomes_de_secao("ficha-rotina.md") == set(SECOES_ROTINA)
    assert nomes_de_secao("readme-schema.md") == set(SECOES_README)
    assert nomes_de_secao("llms.txt") == {"llms"}


def test_paragrafo_do_llms_igual_ao_do_modelo() -> None:
    from pg_atlas.esqueleto_fichas import PARAGRAFO_CONFIANCA

    assert PARAGRAFO_CONFIANCA in (RAIZ_TEMPLATES / "llms.txt").read_text(encoding="utf-8")


def test_nada_especifico_do_banco_no_codigo_do_esqueleto() -> None:
    fontes = Path(esqueleto.__file__).parent.glob("esqueleto*.py")
    for fonte in fontes:
        texto = fonte.read_text(encoding="utf-8").lower()
        for termo in ("el_", "porto", "id_cliente", "eldados"):
            assert termo not in texto, (fonte.name, termo)
