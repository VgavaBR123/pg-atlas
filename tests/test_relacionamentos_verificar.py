"""Testes offline da verificação por amostragem (tarefa 2.4, D-019). Catálogo sintético e conexão falsa."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

import pytest

from pg_atlas import cli, guarda, relacionamentos_verificar as rv
from pg_atlas.config import carregar_projeto
from pg_atlas.erros import ErroConexao, ErroExecucao, ErroConfig
from pg_atlas.verificar_amostragem import (
    Plano,
    calcular_pct,
    filtro_de_tenant,
    linhas_estimadas,
    montar_consulta,
)

BIGINT = "bigint"
AGORA = "2026-01-01T00:00:00+00:00"


# ----------------------------------------------------------------- catálogo sintético


def tab(
    nome: str,
    colunas: dict[str, str] | None = None,
    fks: tuple[dict, ...] = (),
    acesso: str = "ok",
    tipo: str = "tabela",
    particao: dict | None = None,
) -> dict:
    cols = {"id": BIGINT, **(colunas or {})}
    return {
        "nome": nome, "tipo": tipo, "acesso": acesso, "particao": particao, "definicao": None,
        "colunas": [{"nome": c, "tipo": t, "ordem": i} for i, (c, t) in enumerate(cols.items())],
        "restricoes": [{"nome": f"pk_{nome}", "tipo": "pk", "colunas": ["id"]}, *fks],
        "indices": [],
    }


def fk(nome: str, cols: list[str], ref_schema: str, ref_rel: str, ref_cols: list[str], validada: bool = True) -> dict:
    return {
        "nome": nome, "tipo": "fk", "colunas": cols, "ref_schema": ref_schema, "ref_relacao": ref_rel,
        "ref_colunas": ref_cols, "validada": validada, "acao_delete": None, "acao_update": None,
    }


def catalogo(com_tenant: bool = False) -> dict[str, list[dict]]:
    t = {"id_tenant": "uuid"} if com_tenant else {}
    return {
        "a": [
            tab("cliente", {"nome": "text", **t}),
            tab("produto", dict(t)),
            tab(
                "pedido",
                {"id_cliente": BIGINT, "id_cli_alt": BIGINT, "id_cli_nv": BIGINT, **t},
                fks=(
                    fk("fk_ok", ["id_cli_alt"], "a", "cliente", ["id"]),
                    fk("fk_nv", ["id_cli_nv"], "a", "cliente", ["id"], validada=False),
                ),
            ),
            tab("item", {"id_pedido": BIGINT, "id_produto": BIGINT, "id_fechado": BIGINT, **t}),
            tab("fechado", {}, acesso="sem_select"),
        ],
        "b": [tab("produto", dict(t))],
    }


def montar_doc(tmp_path: Path, schemas: dict[str, list[dict]], fatos: str | None = None, projeto_extra: str = "") -> Path:
    doc = tmp_path / "doc"
    (doc / "config").mkdir(parents=True)
    (doc / "config" / "projeto.yml").write_text('projeto:\n  nome: "t"\n' + projeto_extra, encoding="utf-8")
    if fatos is not None:
        (doc / "config" / "fatos.yml").write_text(fatos, encoding="utf-8")
    pasta = doc / "catalogo" / "estrutura"
    pasta.mkdir(parents=True)
    for schema, relacoes in schemas.items():
        (pasta / f"{schema}.json").write_text(
            json.dumps({"formato": "estrutura", "schema": schema, "relacoes": relacoes, "funcoes": []}),
            encoding="utf-8",
        )
    return doc


def preparar(tmp_path: Path, schemas: dict[str, list[dict]] | None = None, fatos: str | None = None, extra: str = "") -> Path:
    doc = montar_doc(tmp_path, schemas or catalogo(), fatos, extra)
    for etapa in ("fks", "nome", "codigo"):
        assert cli.main(["relacionamentos", etapa, "--doc", str(doc)]) == 0
    return doc


def ler_grafo(doc: Path) -> dict[str, Any]:
    return json.loads((doc / "relacionamentos" / "grafo.json").read_text(encoding="utf-8"))


def ler_cache(doc: Path) -> dict[str, Any]:
    return json.loads((doc / "relacionamentos" / "verificacoes.json").read_text(encoding="utf-8"))


def por_id(grafo: dict[str, Any]) -> dict[str, dict]:
    return {a["id"]: a for a in grafo["arestas"]}


def hash_de(caminho: Path) -> str:
    return hashlib.sha256(caminho.read_bytes()).hexdigest()


# ----------------------------------------------------------------- conexão falsa


class ConexaoFalsa:
    """Responde a cada consulta pelo roteiro `(sql) -> (amostrados, casados)` ou lança a exceção devolvida."""

    def __init__(self, roteiro: Callable[[str], Any]) -> None:
        self.roteiro = roteiro
        self.consultas: list[str] = []

    def executar(self, sql: str) -> list[tuple[int, int]]:
        guarda.verificar(sql)  # a Conexao real faria o mesmo
        self.consultas.append(sql)
        resposta = self.roteiro(sql)
        if isinstance(resposta, BaseException):
            raise resposta
        return [resposta]


def sempre(amostrados: int, casados: int) -> Callable[[str], Any]:
    return lambda sql: (amostrados, casados)


def rodar(doc: Path, conexao: ConexaoFalsa, **kw: Any) -> rv.ResultadoRodada:
    projeto = carregar_projeto(doc)
    return rv.executar_verificacao(projeto, conexao=conexao, agora=lambda: AGORA, dormir=lambda s: None, **kw)


def aplicar(doc: Path) -> dict[str, Any]:
    assert cli.main(["relacionamentos", "verificar", "--doc", str(doc), "--aplicar"]) == 0
    return ler_grafo(doc)


ID_PEDIDO = "nome:a.item.id_pedido->a.pedido"
ID_CLI = "nome:a.pedido.id_cliente->a.cliente"
ID_PROD_A = "nome:a.item.id_produto->a.produto"
ID_PROD_B = "nome:a.item.id_produto->b.produto"
ID_NV = "fk:a.pedido.fk_nv"


def so_a_consulta_de(coluna: str, valor: Any, padrao: Any = (100, 100)) -> Callable[[str], Any]:
    """Roteiro: a consulta cuja origem usa `coluna` recebe `valor`; as demais, `padrao`."""
    return lambda sql: valor if f'"{coluna}"' in sql else padrao


# ----------------------------------------------------------------- pct e método


def test_pct_direto_para_origem_pequena_ou_sem_estatistica() -> None:
    assert calcular_pct(None) == ("direto", None)
    assert calcular_pct(0) == ("direto", None)
    assert calcular_pct(49_999) == ("direto", None)


def test_pct_mira_cinco_mil_linhas() -> None:
    assert calcular_pct(1_000_000) == ("tablesample", 0.5)
    assert calcular_pct(50_000) == ("tablesample", 10.0)


def test_pct_respeita_o_minimo_de_um_centesimo() -> None:
    assert calcular_pct(100_000_000_000) == ("tablesample", 0.01)


def stat(paginas: int, linhas: int) -> dict[str, int]:
    return {"paginas": paginas, "linhas_estimadas": linhas}


def test_linhas_estimadas_simples_e_sem_estatistica() -> None:
    est = {("s", "t"): stat(10, 1000), ("s", "neg"): stat(10, -1), ("s", "vazia"): stat(0, 0)}
    assert linhas_estimadas(("s", "t"), False, est, {}) == 1000
    assert linhas_estimadas(("s", "neg"), False, est, {}) is None
    assert linhas_estimadas(("s", "vazia"), False, est, {}) is None
    assert linhas_estimadas(("s", "ausente"), False, est, {}) is None


def test_linhas_estimadas_da_particionada_somam_as_particoes() -> None:
    est = {("s", "p1"): stat(5, 30_000), ("s", "p2"): stat(5, 40_000), ("s", "p3"): stat(0, -1)}
    filhos = {("s", "pai"): [("s", "p1"), ("s", "p2"), ("s", "p3")]}
    assert linhas_estimadas(("s", "pai"), True, est, filhos) == 70_000
    assert linhas_estimadas(("s", "pai"), True, {}, filhos) is None


def test_planejar_usa_o_catalogo(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    ctx = rv.carregar_contexto(doc)
    chave = (("a", "item"), ("id_pedido",), ("a", "pedido"), ("id",))
    assert rv.planejar(chave, ctx) == Plano("direto", None, "nenhum")
    sem = (("a", "item"), ("id_fechado",), ("a", "fechado"), ("id",))
    assert rv.planejar(sem, ctx) is None


# ----------------------------------------------------------------- consulta


CHAVE = (("s1", "filha"), ("c1",), ("s2", "pai"), ("d1",))


def consulta(chave=CHAVE, plano=Plano("direto", None, "nenhum"), iguais=True, tenant=None, maximo=1000) -> str:
    return montar_consulta(chave, plano, iguais, tenant, maximo)


def test_consulta_basica_tem_identificadores_entre_aspas_e_so_contagens() -> None:
    sql = consulta()
    assert 'FROM "s1"."filha" f' in sql and 'FROM "s2"."pai" p' in sql
    assert 'f."c1" IS NOT NULL' in sql and 'p."d1" = a.v0' in sql
    assert "LIMIT 1000" in sql and "TABLESAMPLE" not in sql and "::text" not in sql
    assert "count(*) AS amostrados" in sql and "count(*) FILTER (WHERE EXISTS" in sql
    guarda.verificar(sql)


def test_consulta_tablesample_com_semente_fixa() -> None:
    sql = consulta(plano=Plano("tablesample", 0.5, "nenhum"))
    assert "f TABLESAMPLE SYSTEM (0.5000) REPEATABLE (42) WHERE" in sql
    guarda.verificar(sql)


def test_consulta_de_chave_composta_usa_and_em_todas_as_colunas() -> None:
    chave = (("s1", "filha"), ("c1", "c2"), ("s2", "pai"), ("d1", "d2"))
    sql = consulta(chave)
    assert 'f."c1" IS NOT NULL AND f."c2" IS NOT NULL' in sql
    assert 'p."d1" = a.v0 AND p."d2" = a.v1' in sql


def test_consulta_com_tipos_diferentes_converte_para_texto_dos_dois_lados() -> None:
    sql = consulta(iguais=False)
    assert 'f."c1"::text AS v0' in sql and 'p."d1"::text = a.v0' in sql


def test_consulta_com_tenant_correlacionado() -> None:
    sql = consulta(plano=Plano("direto", None, "correlacionado"), tenant="tn")
    assert 'f."tn" AS t' in sql and 'f."tn" IS NOT NULL' in sql and 'p."tn" = a.t' in sql
    guarda.verificar(sql)


def test_consulta_sem_tenant_nao_menciona_a_coluna() -> None:
    sql = consulta(plano=Plano("direto", None, "nenhum"), tenant="tn")
    assert '"tn"' not in sql


def test_identificador_com_aspas_e_escapado() -> None:
    chave = (("s", 'fi"lha'), ("c",), ("s", "pai"), ("d",))
    assert '"fi""lha"' in consulta(chave)


def test_filtro_de_tenant_exige_coluna_nos_dois_lados_com_mesmo_tipo() -> None:
    o, d = {"t": "uuid", "c": "int"}, {"t": "uuid", "d": "int"}
    assert filtro_de_tenant("t", o, d, ("c",), ("d",)) == "correlacionado"
    assert filtro_de_tenant(None, o, d, ("c",), ("d",)) == "nenhum"
    assert filtro_de_tenant("t", o, {"d": "int"}, ("c",), ("d",)) == "nenhum"
    assert filtro_de_tenant("t", o, {"t": "text", "d": "int"}, ("c",), ("d",)) == "nenhum"
    assert filtro_de_tenant("t", o, d, ("t",), ("t",)) == "nenhum"


def test_guarda_continua_barrando_escrita_mesmo_com_tablesample() -> None:
    ok, _ = guarda.permitido('SELECT count(*) FROM "s"."t" f TABLESAMPLE SYSTEM (1) REPEATABLE (1)')
    assert ok
    assert not guarda.permitido('DELETE FROM "s"."t" f')[0]
    assert not guarda.permitido('SELECT most_common_vals FROM pg_stats')[0]


# ----------------------------------------------------------------- pares e cache


def test_pares_unicos_incluem_hipoteses_e_fk_nv_e_nao_as_fks_validas(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    pares = rv.coletar_pares(ler_grafo(doc)["arestas"])
    origens = {(k[0][1], k[1][0], k[2][0] + "." + k[2][1]) for k in pares}
    assert ("pedido", "id_cli_nv", "a.cliente") in origens
    assert ("pedido", "id_cli_alt", "a.cliente") not in origens
    assert ("item", "id_pedido", "a.pedido") in origens
    assert len(pares) == len(set(pares))


def test_par_com_duas_fontes_e_amostrado_uma_vez(tmp_path: Path) -> None:
    schemas = catalogo()
    schemas["a"].append(tab("v", {"x": "int"}))
    doc = preparar(tmp_path)
    grafo = ler_grafo(doc)
    copia = json.loads(json.dumps(grafo["arestas"][0]))
    a = next(x for x in grafo["arestas"] if x["id"] == ID_PEDIDO)
    dup = json.loads(json.dumps(a)) | {"id": "codigo:dup", "fonte": "inferencia_codigo",
                                       "evidencia": [{"tipo": "inferencia_codigo", "tipos_iguais": True}]}
    pares = rv.coletar_pares([a, dup, copia])
    assert sum(1 for k in pares if k[0] == ("a", "item") and k[1] == ("id_pedido",)) == 1


def test_codigo_com_tipos_diferentes_marca_o_par() -> None:
    a = {
        "fonte": "inferencia_codigo", "evidencia": [{"tipo": "inferencia_codigo", "tipos_iguais": False}],
        "origem": {"schema": "s", "relacao": "f", "colunas": ["c"]},
        "destino": {"schema": "s", "relacao": "p", "colunas": ["d"]},
    }
    assert list(rv.coletar_pares([a]).values()) == [False]


def test_cache_e_gravado_ordenado_e_valida_contra_o_esquema(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    rodar(doc, ConexaoFalsa(sempre(100, 100)))
    dados = ler_cache(doc)
    chaves = [rv.chave_do_registro(r) for r in dados["registros"]]
    assert chaves == sorted(chaves) and len(chaves) == len(set(chaves))
    rv.validar_cache(dados)
    r = dados["registros"][0]
    assert set(r) == {
        "origem", "destino", "versao_metodo", "metodo", "pct", "filtro_tenant", "amostrados", "casados",
        "taxa", "situacao", "erro", "verificado_em",
    }
    assert r["versao_metodo"] == 1 and r["verificado_em"] == AGORA


def test_esquema_do_cache_nao_aceita_campos_extras(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    rodar(doc, ConexaoFalsa(sempre(100, 100)))
    dados = ler_cache(doc)
    dados["registros"][0]["valores"] = [1, 2]
    with pytest.raises(Exception):
        rv.validar_cache(dados)


def test_cache_invalido_nao_e_sobrescrito(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    caminho = doc / "relacionamentos" / "verificacoes.json"
    caminho.write_text("{", encoding="utf-8")
    with pytest.raises(Exception):
        rodar(doc, ConexaoFalsa(sempre(100, 100)))
    assert caminho.read_text(encoding="utf-8") == "{"


# ----------------------------------------------------------------- limiares e aplicação


def test_taxa_alta_confirma_e_grava_so_taxa_e_contagens(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    rodar(doc, ConexaoFalsa(so_a_consulta_de("id_pedido", (200, 190))))  # 0,95 exato
    g = aplicar(doc)
    a = por_id(g)[ID_PEDIDO]
    assert a["confianca"] == "confirmado" and a["taxa_casamento"] == 0.95
    ev = a["evidencia"][-1]
    assert ev == {"tipo": "amostragem", "amostrados": 200, "casados": 190, "metodo": "direto", "pct": None,
                  "filtro_tenant": "nenhum"}
    assert a["nota"] is None


def test_taxa_intermediaria_vira_hipotese_com_nota(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    rodar(doc, ConexaoFalsa(so_a_consulta_de("id_pedido", (100, 70))))
    a = por_id(aplicar(doc))[ID_PEDIDO]
    assert a["confianca"] == "hipotese" and a["taxa_casamento"] == 0.7
    assert a["nota"] == "taxa de casamento abaixo de 95%"


def test_taxa_baixa_descarta_a_aresta_mas_mantem_no_cache(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    rodar(doc, ConexaoFalsa(so_a_consulta_de("id_pedido", (100, 49))))
    g = aplicar(doc)
    assert ID_PEDIDO not in por_id(g)
    assert g["resumo"]["verificacao"]["descartadas"] == 1
    assert any(r["taxa"] == 0.49 for r in ler_cache(doc)["registros"])


def test_limiares_vem_do_projeto(tmp_path: Path) -> None:
    extra = "execucao:\n  verificacoes:\n    taxa_confirmado: 0.8\n    taxa_hipotese: 0.6\n    amostra_min_valores: 10\n"
    doc = preparar(tmp_path, extra=extra)
    rodar(doc, ConexaoFalsa(so_a_consulta_de("id_pedido", (100, 85))))
    a = por_id(aplicar(doc))[ID_PEDIDO]
    assert a["confianca"] == "confirmado"


def test_limiar_invalido_e_rejeitado(tmp_path: Path) -> None:
    extra = "execucao:\n  verificacoes:\n    taxa_confirmado: 0.4\n    taxa_hipotese: 0.6\n"
    doc = preparar(tmp_path, extra=extra)
    with pytest.raises(ErroConfig):
        rodar(doc, ConexaoFalsa(sempre(100, 100)))


def test_amostra_pequena_grava_taxa_sem_confirmar_nem_descartar(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    rodar(doc, ConexaoFalsa(so_a_consulta_de("id_cliente", (5, 0))))
    g = aplicar(doc)
    a = por_id(g)[ID_CLI]
    assert a["confianca"] == "hipotese" and a["taxa_casamento"] == 0.0
    assert a["nota"] == "amostra pequena (5 valores)"
    assert g["resumo"]["verificacao"]["amostra_pequena"] >= 1


def test_amostra_vazia(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    rodar(doc, ConexaoFalsa(so_a_consulta_de("id_pedido", (0, 0))))
    g = aplicar(doc)
    a = por_id(g)[ID_PEDIDO]
    assert a["confianca"] == "hipotese" and a["taxa_casamento"] is None
    assert a["nota"] == "sem verificação: amostra vazia"
    reg = next(r for r in ler_cache(doc)["registros"] if r["origem"]["colunas"] == ["id_pedido"])
    assert reg["situacao"] == "amostra_vazia" and reg["taxa"] is None


def test_sem_select_nao_consulta(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    falsa = ConexaoFalsa(sempre(100, 100))
    rodar(doc, falsa)
    assert not any('"fechado"' in s for s in falsa.consultas)
    reg = next(r for r in ler_cache(doc)["registros"] if r["destino"]["relacao"] == "fechado")
    assert reg["situacao"] == "sem_select" and reg["metodo"] is None and reg["amostrados"] == 0
    a = por_id(aplicar(doc))["nome:a.item.id_fechado->a.fechado"]
    assert a["confianca"] == "hipotese" and a["nota"] == "sem verificação: sem permissão de leitura"


@pytest.mark.parametrize(
    "erro, classe",
    [(ErroExecucao("x", "42P01"), "42P01"), (ErroExecucao("texto do servidor"), "ErroExecucao")],
)
def test_erro_de_execucao_grava_so_a_classe(tmp_path: Path, erro: ErroExecucao, classe: str) -> None:
    doc = preparar(tmp_path)
    rodar(doc, ConexaoFalsa(so_a_consulta_de("id_pedido", erro)))
    reg = next(r for r in ler_cache(doc)["registros"] if r["origem"]["colunas"] == ["id_pedido"])
    assert reg["situacao"] == "erro" and reg["erro"] == classe and reg["taxa"] is None
    texto = (doc / "relacionamentos" / "verificacoes.json").read_text(encoding="utf-8")
    assert "texto do servidor" not in texto
    g = aplicar(doc)
    assert por_id(g)[ID_PEDIDO]["nota"] == f"sem verificação: erro ({classe})"
    assert g["resumo"]["verificacao"]["erros"] == {classe: 1}


def test_sql_rejeitado_pelo_guarda_vira_erro_do_par(tmp_path: Path) -> None:
    from pg_atlas.erros import SqlRejeitado

    doc = preparar(tmp_path)
    rodar(doc, ConexaoFalsa(so_a_consulta_de("id_pedido", SqlRejeitado("não"))))
    reg = next(r for r in ler_cache(doc)["registros"] if r["origem"]["colunas"] == ["id_pedido"])
    assert reg["erro"] == "SqlRejeitado"


# ----------------------------------------------------------------- FK NOT VALID


def test_fk_not_valid_recebe_taxa_e_continua_declarada(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    rodar(doc, ConexaoFalsa(so_a_consulta_de("id_cli_nv", (200, 150))))
    g = aplicar(doc)
    a = por_id(g)[ID_NV]
    assert a["confianca"] == "declarado" and a["taxa_casamento"] == 0.75
    assert a["nota"].endswith("; taxa de casamento medida: 0.7500")
    assert a["nota"].startswith("FK declarada NOT VALID")
    assert g["resumo"]["verificacao"]["not_valid_medidas"] == 1
    assert por_id(g)["fk:a.pedido.fk_ok"]["taxa_casamento"] is None


def test_fk_not_valid_com_taxa_baixa_nao_e_removida(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    rodar(doc, ConexaoFalsa(so_a_consulta_de("id_cli_nv", (200, 10))))
    assert ID_NV in por_id(aplicar(doc))


def test_toda_fk_declarada_continua_no_grafo(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    rodar(doc, ConexaoFalsa(sempre(100, 0)))
    ids = {a["id"] for a in aplicar(doc)["arestas"] if a["fonte"] == "fk_declarada"}
    assert ids == {"fk:a.pedido.fk_ok", ID_NV}


# ----------------------------------------------------------------- ambiguidade


def test_grafo_inicial_tem_produto_ambiguo(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    nos = por_id(ler_grafo(doc))
    assert nos[ID_PROD_A]["nota"] == "ambíguo: 2 destinos possíveis"
    assert nos[ID_PROD_B]["nota"] == "ambíguo: 2 destinos possíveis"


def roteiro_produto(a: tuple[int, int], b: tuple[int, int]) -> Callable[[str], Any]:
    def r(sql: str) -> Any:
        if '"id_produto"' in sql:
            return a if 'FROM "a"."produto" p' in sql else b
        return (100, 100)

    return r


def test_desempate_quando_um_destino_confirma_e_o_outro_e_descartado(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    rodar(doc, ConexaoFalsa(roteiro_produto((100, 99), (100, 10))))
    g = aplicar(doc)
    nos = por_id(g)
    assert ID_PROD_B not in nos
    a = nos[ID_PROD_A]
    assert a["confianca"] == "confirmado" and a["nota"] is None
    assert a["evidencia"][-1]["desempate"] == "amostragem"
    assert g["resumo"]["verificacao"]["desempatadas"] == 1


def test_ambigua_reduzida_quando_o_unico_sobrevivente_e_hipotese(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    rodar(doc, ConexaoFalsa(roteiro_produto((100, 80), (100, 10))))
    g = aplicar(doc)
    a = por_id(g)[ID_PROD_A]
    assert a["confianca"] == "hipotese" and a["nota"] == "taxa de casamento abaixo de 95%"
    assert g["resumo"]["verificacao"]["ambiguas_reduzidas"] == 1
    assert g["resumo"]["verificacao"]["desempatadas"] == 0


def test_ambiguidade_permanece_quando_os_dois_sobrevivem(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    rodar(doc, ConexaoFalsa(roteiro_produto((100, 99), (100, 99))))
    nos = por_id(aplicar(doc))
    for i in (ID_PROD_A, ID_PROD_B):
        assert nos[i]["confianca"] == "confirmado"
        assert nos[i]["nota"] == "ambíguo: 2 destinos possíveis"
        assert "desempate" not in nos[i]["evidencia"][-1]


def test_n_recalculado_com_tres_candidatos(tmp_path: Path) -> None:
    schemas = catalogo()
    schemas["c"] = [tab("produto")]
    doc = preparar(tmp_path, schemas)
    assert por_id(ler_grafo(doc))["nome:a.item.id_produto->c.produto"]["nota"] == "ambíguo: 3 destinos possíveis"

    def r(sql: str) -> Any:
        if '"id_produto"' in sql and 'FROM "c"."produto" p' in sql:
            return (100, 5)
        return (100, 99)

    rodar(doc, ConexaoFalsa(r))
    nos = por_id(aplicar(doc))
    assert "nome:a.item.id_produto->c.produto" not in nos
    assert nos[ID_PROD_A]["nota"] == "ambíguo: 2 destinos possíveis"


# ----------------------------------------------------------------- idempotência e determinismo


def test_aplicar_duas_vezes_da_o_mesmo_arquivo(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    rodar(doc, ConexaoFalsa(roteiro_produto((100, 99), (100, 10))))
    primeiro = aplicar(doc)
    h1 = hash_de(doc / "relacionamentos" / "grafo.json")
    segundo = aplicar(doc)
    assert primeiro == segundo and hash_de(doc / "relacionamentos" / "grafo.json") == h1


def test_aplicar_apos_execucao_completa_e_idempotente_com_notas_combinadas(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    rodar(doc, ConexaoFalsa(roteiro_produto((100, 80), (100, 80))))
    g1 = aplicar(doc)
    a = por_id(g1)[ID_PROD_A]
    assert a["nota"] == "ambíguo: 2 destinos possíveis; taxa de casamento abaixo de 95%"
    h = hash_de(doc / "relacionamentos" / "grafo.json")
    aplicar(doc)
    assert hash_de(doc / "relacionamentos" / "grafo.json") == h


def test_mudar_limiar_e_reaplicar_muda_o_resultado_sem_duplicar_notas(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    rodar(doc, ConexaoFalsa(so_a_consulta_de("id_pedido", (100, 70))))
    aplicar(doc)
    (doc / "config" / "projeto.yml").write_text(
        'projeto:\n  nome: "t"\nexecucao:\n  verificacoes:\n    taxa_confirmado: 0.7\n    taxa_hipotese: 0.5\n',
        encoding="utf-8",
    )
    a = por_id(aplicar(doc))[ID_PEDIDO]
    assert a["confianca"] == "confirmado" and a["nota"] is None
    assert len([e for e in a["evidencia"] if e["tipo"] == "amostragem"]) == 1


def test_grafo_depende_so_do_cache(tmp_path: Path) -> None:
    doc1 = preparar(tmp_path / "um")
    doc2 = preparar(tmp_path / "dois")
    rodar(doc1, ConexaoFalsa(roteiro_produto((100, 99), (100, 10))))
    (doc2 / "relacionamentos" / "verificacoes.json").write_bytes((doc1 / "relacionamentos" / "verificacoes.json").read_bytes())
    aplicar(doc1)
    aplicar(doc2)
    assert (doc1 / "relacionamentos" / "grafo.json").read_bytes() == (doc2 / "relacionamentos" / "grafo.json").read_bytes()


def test_grafo_aplicado_valida_no_esquema_e_nao_tem_horario(tmp_path: Path) -> None:
    from pg_atlas.relacionamentos import validar

    doc = preparar(tmp_path)
    rodar(doc, ConexaoFalsa(sempre(100, 97)))
    g = aplicar(doc)
    validar(g)
    assert "verificado_em" not in json.dumps(g)
    assert g["resumo"]["verificacao"]["versao_metodo"] == 1


def test_cada_confirmada_tem_taxa_registrada(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    rodar(doc, ConexaoFalsa(sempre(100, 97)))
    for a in aplicar(doc)["arestas"]:
        if a["confianca"] == "confirmado":
            assert a["taxa_casamento"] is not None


# ----------------------------------------------------------------- retomada, versão, limite


def test_retomar_nao_reamostra(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    primeira = ConexaoFalsa(sempre(100, 100))
    r1 = rodar(doc, primeira)
    segunda = ConexaoFalsa(sempre(100, 100))
    r2 = rodar(doc, segunda)
    assert primeira.consultas and not segunda.consultas and r2.processados == 0
    assert r1.pendentes == 0


def test_refazer_reamostra_tudo(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    rodar(doc, ConexaoFalsa(sempre(100, 100)))
    de_novo = ConexaoFalsa(sempre(100, 100))
    rodar(doc, de_novo, refazer=True)
    assert de_novo.consultas


def test_versao_antiga_e_reamostrada_sem_refazer(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    rodar(doc, ConexaoFalsa(sempre(100, 100)))
    caminho = doc / "relacionamentos" / "verificacoes.json"
    dados = ler_cache(doc)
    dados["registros"][0]["versao_metodo"] = 99
    caminho.write_text(json.dumps(dados), encoding="utf-8")
    nova = ConexaoFalsa(sempre(100, 100))
    r = rodar(doc, nova)
    assert r.processados == 1  # só o par de versão antiga volta
    assert all(x["versao_metodo"] == 1 for x in ler_cache(doc)["registros"])


def test_registro_de_versao_antiga_e_ignorado_na_aplicacao(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    rodar(doc, ConexaoFalsa(sempre(100, 100)))
    dados = ler_cache(doc)
    for r in dados["registros"]:
        r["versao_metodo"] = 99
    (doc / "relacionamentos" / "verificacoes.json").write_text(json.dumps(dados), encoding="utf-8")
    g = aplicar(doc)
    assert g["resumo"]["verificacao"]["pendentes"] == g["resumo"]["verificacao"]["pares"]
    assert "confirmado" not in g["resumo"]["por_confianca"]


def test_limite_corta_a_rodada_e_a_seguinte_continua(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    r1 = rodar(doc, ConexaoFalsa(sempre(100, 100)), limite=2)
    assert r1.processados == 2 and r1.pendentes > 0
    assert len(ler_cache(doc)["registros"]) == 2
    r2 = rodar(doc, ConexaoFalsa(sempre(100, 100)))
    assert r2.pendentes == 0 and r2.processados == r1.pendentes


def test_pausa_entre_consultas(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    pausas: list[float] = []
    projeto = carregar_projeto(doc)
    rv.executar_verificacao(
        projeto, conexao=ConexaoFalsa(sempre(100, 100)), pausa_ms=250, limite=3, agora=lambda: AGORA,
        dormir=pausas.append,
    )
    assert pausas and set(pausas) == {0.25}


def test_pares_que_saem_do_grafo_ficam_no_cache(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    rodar(doc, ConexaoFalsa(sempre(100, 100)))
    dados = ler_cache(doc)
    extra = json.loads(json.dumps(dados["registros"][0]))
    extra["origem"]["relacao"] = "sumiu"
    dados["registros"].append(extra)
    (doc / "relacionamentos" / "verificacoes.json").write_text(json.dumps(dados), encoding="utf-8")
    rodar(doc, ConexaoFalsa(sempre(100, 100)))
    assert any(r["origem"]["relacao"] == "sumiu" for r in ler_cache(doc)["registros"])
    aplicar(doc)


# ----------------------------------------------------------------- freio de carga e interrupção


@pytest.mark.parametrize("sqlstate, tipo", [("57014", "timeout"), ("55P03", "lock")])
def test_tres_falhas_seguidas_param_a_rodada(tmp_path: Path, sqlstate: str, tipo: str) -> None:
    doc = preparar(tmp_path)
    falsa = ConexaoFalsa(lambda sql: ErroExecucao("x", sqlstate))
    r = rodar(doc, falsa)
    assert r.freio == tipo and len(falsa.consultas) == 3
    assert [x["situacao"] for x in ler_cache(doc)["registros"]] == ["sem_select"]  # os três do freio ficam pendentes
    retomada = ConexaoFalsa(sempre(100, 100))
    assert rodar(doc, retomada).freio is None and len(retomada.consultas) >= 3


def test_falhas_de_tipos_diferentes_nao_disparam_o_freio(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    n = {"i": 0}

    def r(sql: str) -> Any:
        n["i"] += 1
        return ErroExecucao("x", "57014" if n["i"] % 2 else "55P03")

    resultado = rodar(doc, ConexaoFalsa(r))
    assert resultado.freio is None and resultado.situacoes["erro"] == 5


def test_sucesso_no_meio_zera_a_sequencia(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    n = {"i": 0}

    def r(sql: str) -> Any:
        n["i"] += 1
        return (100, 100) if n["i"] % 3 == 0 else ErroExecucao("x", "57014")

    resultado = rodar(doc, ConexaoFalsa(r))
    assert resultado.freio is None and resultado.processados == 6


def test_interrupcao_grava_o_cache_e_propaga(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    n = {"i": 0}

    def r(sql: str) -> Any:
        n["i"] += 1
        if n["i"] == 3:
            raise KeyboardInterrupt
        return (100, 100)

    with pytest.raises(KeyboardInterrupt):
        rodar(doc, ConexaoFalsa(r))
    assert len(ler_cache(doc)["registros"]) == 3  # o sem_select e dois pares consultados


def test_erro_de_conexao_grava_o_cache_e_propaga(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    n = {"i": 0}

    def r(sql: str) -> Any:
        n["i"] += 1
        return ErroConexao("caiu") if n["i"] == 2 else (100, 100)

    with pytest.raises(ErroConexao):
        rodar(doc, ConexaoFalsa(r))
    assert len(ler_cache(doc)["registros"]) == 2  # o sem_select e um par consultado


def test_cache_e_gravado_a_cada_cem_pares(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    doc = montar_doc(tmp_path, {"s": [tab("p"), *[tab(f"f{i}", {"p_id": BIGINT}) for i in range(1, 251)], tab("g", {"q": BIGINT}, fks=(fk("fk_g", ["q"], "s", "p", ["id"]),))]})
    for etapa in ("fks", "nome"):
        assert cli.main(["relacionamentos", etapa, "--doc", str(doc)]) == 0
    gravacoes: list[int] = []
    original = rv.gravar_cache

    def espiao(caminho: Path, cache: dict) -> None:
        gravacoes.append(len(cache))
        original(caminho, cache)

    monkeypatch.setattr(rv, "gravar_cache", espiao)
    rodar(doc, ConexaoFalsa(sempre(100, 100)))
    assert gravacoes[:2] == [100, 200] and gravacoes[-1] == 250


def test_nada_pendente_nao_conecta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    doc = preparar(tmp_path)
    rodar(doc, ConexaoFalsa(sempre(100, 100)))

    def nao_deve_conectar(*a: Any, **k: Any) -> Any:
        raise AssertionError("conectou sem necessidade")

    monkeypatch.setattr(rv, "Conexao", nao_deve_conectar)
    assert cli.main(["relacionamentos", "verificar", "--doc", str(doc)]) == 0


# ----------------------------------------------------------------- tenant e método


def test_tenant_correlacionado_quando_declarado_nos_dois_lados(tmp_path: Path) -> None:
    doc = preparar(tmp_path, catalogo(com_tenant=True), fatos="tenant:\n  coluna: id_tenant\n")
    falsa = ConexaoFalsa(sempre(100, 100))
    rodar(doc, falsa)
    com = [s for s in falsa.consultas if 'FROM "a"."item" f' in s and '"id_pedido"' in s][0]
    assert 'f."id_tenant" AS t' in com and 'p."id_tenant" = a.t' in com
    assert all(r["filtro_tenant"] == "correlacionado" for r in ler_cache(doc)["registros"] if r["metodo"])
    g = aplicar(doc)
    assert por_id(g)[ID_PEDIDO]["evidencia"][-1]["filtro_tenant"] == "correlacionado"
    assert g["resumo"]["verificacao"]["por_filtro_tenant"]["correlacionado"] >= 1


def test_sem_tenant_declarado_nao_filtra(tmp_path: Path) -> None:
    doc = preparar(tmp_path, catalogo(com_tenant=True))
    falsa = ConexaoFalsa(sempre(100, 100))
    rodar(doc, falsa)
    assert not any("id_tenant" in s for s in falsa.consultas)
    assert {r["filtro_tenant"] for r in ler_cache(doc)["registros"]} == {"nenhum"}


def test_tablesample_para_origem_grande(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    pasta = doc / "catalogo" / "estatisticas"
    pasta.mkdir()
    (pasta / "a.json").write_text(
        json.dumps({"formato": "estatisticas", "relacoes": [
            {"nome": "item", "tipo": "tabela", "paginas": 9000, "linhas_estimadas": 1_000_000}]}),
        encoding="utf-8",
    )
    falsa = ConexaoFalsa(sempre(100, 100))
    rodar(doc, falsa)
    sql = next(s for s in falsa.consultas if 'FROM "a"."item" f' in s)
    assert "TABLESAMPLE SYSTEM (0.5000) REPEATABLE (42)" in sql
    reg = next(r for r in ler_cache(doc)["registros"] if r["origem"]["relacao"] == "item" and r["metodo"])
    assert reg["metodo"] == "tablesample" and reg["pct"] == 0.5
    ev = por_id(aplicar(doc))[ID_PEDIDO]["evidencia"][-1]
    assert ev["metodo"] == "tablesample" and ev["pct"] == 0.5


def test_amostra_max_vem_do_projeto(tmp_path: Path) -> None:
    doc = preparar(tmp_path, extra="execucao:\n  verificacoes:\n    amostra_max_valores: 250\n")
    falsa = ConexaoFalsa(sempre(100, 100))
    rodar(doc, falsa)
    assert falsa.consultas and all("LIMIT 250)" in s for s in falsa.consultas)


# ----------------------------------------------------------------- remoção do bloco e linha de comando


@pytest.mark.parametrize("etapa", ["fks", "nome", "codigo"])
def test_etapas_proprias_removem_o_bloco_e_avisam(
    tmp_path: Path, etapa: str, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = preparar(tmp_path)
    rodar(doc, ConexaoFalsa(sempre(100, 100)))
    assert "verificacao" in aplicar(doc)["resumo"]
    capsys.readouterr()
    assert cli.main(["relacionamentos", etapa, "--doc", str(doc)]) == 0
    assert "verificacao" not in ler_grafo(doc)["resumo"]
    assert "relacionamentos verificar --aplicar" in capsys.readouterr().err
    assert "verificacao" in aplicar(doc)["resumo"]  # reaplicar o cache refaz o bloco


def test_sem_bloco_nao_ha_aviso(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    doc = preparar(tmp_path)
    capsys.readouterr()
    assert cli.main(["relacionamentos", "nome", "--doc", str(doc)]) == 0
    assert "verificar --aplicar" not in capsys.readouterr().err


def test_aplicar_sem_cache_deixa_tudo_pendente(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    antes = {a["id"]: a for a in ler_grafo(doc)["arestas"]}
    g = aplicar(doc)
    assert {a["id"] for a in g["arestas"]} == set(antes)
    assert g["resumo"]["verificacao"]["pendentes"] == g["resumo"]["verificacao"]["pares"] > 0
    assert all(a["nota"] == antes[a["id"]]["nota"] for a in g["arestas"])


def test_aplicar_nao_conecta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    doc = preparar(tmp_path)

    def nao_deve_conectar(*a: Any, **k: Any) -> Any:
        raise AssertionError("conectou")

    monkeypatch.setattr(rv, "Conexao", nao_deve_conectar)
    aplicar(doc)


class ConexaoContexto(ConexaoFalsa):
    def __enter__(self) -> ConexaoFalsa:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def test_cli_freio_sai_com_codigo_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = preparar(tmp_path)
    monkeypatch.setattr(rv, "Conexao", lambda projeto: ConexaoContexto(lambda sql: ErroExecucao("x", "57014")))
    assert cli.main(["relacionamentos", "verificar", "--doc", str(doc)]) == 2
    assert "carga ou contenção" in capsys.readouterr().err
    assert "verificacao" not in ler_grafo(doc)["resumo"]


def test_cli_rodada_normal_imprime_tempo_e_contagens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = preparar(tmp_path)
    monkeypatch.setattr(rv, "Conexao", lambda projeto: ConexaoContexto(sempre(100, 100)))
    assert cli.main(["relacionamentos", "verificar", "--doc", str(doc), "--limite", "3"]) == 0
    saida = capsys.readouterr().out
    assert "pares verificados=3" in saida and "média por consulta" in saida and "verificado=" in saida


def test_cli_rejeita_limite_negativo(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    with pytest.raises(SystemExit):
        cli.main(["relacionamentos", "verificar", "--doc", str(doc), "--limite", "-1"])


def test_cache_nao_guarda_valores_de_dado(tmp_path: Path) -> None:
    doc = preparar(tmp_path)
    rodar(doc, ConexaoFalsa(sempre(100, 97)))
    chaves: set[str] = set()
    for r in ler_cache(doc)["registros"]:
        chaves |= set(r)
    assert not {"valores", "amostra", "exemplos"} & chaves


# ----------------------------------------------------------------- banco real (opcional)


@pytest.mark.banco
def test_banco_amostra_poucos_pares(tmp_path: Path) -> None:
    """Amostra 3 pares no banco real, numa cópia do grafo (exige `PGATLAS_DOC`). Verifica só contagens."""
    import os
    import shutil

    from pg_atlas.config import Projeto

    origem = os.environ.get("PGATLAS_DOC")
    if not origem:
        pytest.skip("defina PGATLAS_DOC com o caminho do repositório de documentação")
    real = carregar_projeto(origem)
    copia = tmp_path / "doc"
    shutil.copytree(real.doc / "config", copia / "config")
    shutil.copytree(real.doc / "catalogo", copia / "catalogo")
    shutil.copytree(real.doc / "relacionamentos", copia / "relacionamentos")
    (copia / "_controle").mkdir()
    resultado = rv.executar_verificacao(Projeto(doc=copia, workspace=real.workspace, dados=real.dados), limite=3)
    assert resultado.processados == 3
    rv.validar_cache(json.loads((copia / "relacionamentos" / "verificacoes.json").read_text(encoding="utf-8")))
