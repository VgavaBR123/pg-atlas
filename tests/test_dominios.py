"""Testes offline do `dominios ler` (D-023, item 8): guarda no modo de leitura de valores, consulta,
máscara, retomada, saída determinística e incorporação ao pacote de contexto. Dados sintéticos."""

from __future__ import annotations

import datetime
import decimal
import json
from collections.abc import Collection
from pathlib import Path
from typing import Any

import pytest

from pg_atlas import contexto_lotes as ctx
from pg_atlas import dominios as dom
from pg_atlas.conexao import Conexao
from pg_atlas.config import carregar_projeto
from pg_atlas.erros import ErroConexao, ErroExecucao, SqlRejeitado
from pg_atlas.guarda import permitido, verificar
from pg_atlas.json_util import escrever_json
from test_contexto_lotes import montar_doc, rodar

LIB = ["s.dom", "s.outra"]


# ----------------------------------------------------------------- guarda


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT "id", left("k", 201) AS "k" FROM "s"."dom" ORDER BY "id" LIMIT 500',
        "SELECT id FROM s.dom LIMIT 5",
        "SELECT * FROM s.outra",
    ],
)
def test_guarda_aceita_tabela_liberada(sql: str) -> None:
    verificar(sql, LIB)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM s.cliente",  # fora da lista
        "SELECT id FROM dom",  # sem schema
        'SELECT id FROM "S"."dom"',  # caixa diferente: nome exato
        "SELECT id FROM s.dom JOIN s.cliente USING (id)",
        "SELECT id FROM s.dom, s.cliente",
        "SELECT id FROM s.dom WHERE id IN (SELECT id FROM s.cliente)",
        "SELECT (SELECT max(id) FROM s.cliente) FROM s.dom",
        "SELECT id FROM s.dom UNION SELECT id FROM s.cliente",
        "WITH x AS (SELECT id FROM s.cliente) SELECT id FROM x",
        "WITH x AS (SELECT id FROM s.dom) SELECT id FROM x",
        "SELECT * FROM generate_series(1, 3)",
        "SELECT 1",
        "SELECT * FROM pg_stats",
        "SELECT count(*) FROM pg_stat_activity",
        "EXPLAIN SELECT id FROM s.dom",
        "SHOW server_version",
        "SELECT id FROM s.dom; SELECT id FROM s.cliente",
        "SELECT id FROM db.s.dom",
        "SELECT id FROM s.dom FOR UPDATE",
    ],
)
def test_guarda_recusa_o_que_nao_e_tabela_liberada(sql: str) -> None:
    assert not permitido(sql, LIB)[0]


def test_guarda_sem_lista_mantem_comportamento_anterior() -> None:
    assert permitido("SELECT count(*) FROM s.cliente")[0]
    assert not permitido("SELECT count(*) FROM s.cliente", [])[0]  # lista vazia: nada liberado
    with pytest.raises(SqlRejeitado):
        verificar("DELETE FROM s.dom", LIB)


class _Cur:
    def __init__(self, base: _Conn) -> None:
        self.base = base
        self.description = [("id",)]

    def __enter__(self) -> _Cur:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def execute(self, sql: str, params: Any = None) -> None:
        self.base.executados.append(sql)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return [(1,)]


class _Conn:
    def __init__(self) -> None:
        self.executados: list[str] = []

    def cursor(self) -> _Cur:
        return _Cur(self)

    def rollback(self) -> None:
        pass


def test_conexao_aplica_a_lista_no_guarda(doc: Path) -> None:
    conexao = Conexao(carregar_projeto(doc))
    conexao._conn = _Conn()
    colunas, linhas = conexao.executar_com_colunas("SELECT id FROM s.dom", tabelas_liberadas=["s.dom"])
    assert linhas == [(1,)] and colunas == ["id"]
    with pytest.raises(SqlRejeitado):
        conexao.executar_com_colunas("SELECT id FROM s.cliente", tabelas_liberadas=["s.dom"])
    assert conexao._conn.executados == ["SELECT id FROM s.dom"]  # a rejeitada não chegou ao banco


# ----------------------------------------------------------------- consulta


def relacao(nome: str = "dom", **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "nome": nome, "tipo": "tabela", "acesso": "ok",
        "colunas": [
            {"nome": "id", "tipo": "bigint", "ordem": 1},
            {"nome": "descricao", "tipo": "character varying(300)", "ordem": 2},
            {"nome": "anexo", "tipo": "bytea", "ordem": 3},
            {"nome": "extra", "tipo": "jsonb", "ordem": 4},
        ],
        "restricoes": [{"nome": f"pk_{nome}", "tipo": "pk", "colunas": ["id"]}],
    }
    base.update(extra)
    return base


def test_sql_omite_tipos_grandes_corta_texto_e_ordena_pela_pk() -> None:
    rel = relacao()
    lidas, omitidas = dom.separar_colunas(rel)
    assert [c["nome"] for c in lidas] == ["id", "descricao"]
    assert omitidas == [{"nome": "anexo", "tipo": "bytea"}, {"nome": "extra", "tipo": "jsonb"}]
    ordem = dom.ordem_da_consulta(rel, lidas)
    sql = dom.montar_sql("s", "dom", lidas, ordem, 500)
    assert sql == 'SELECT "id", left("descricao", 201) AS "descricao" FROM "s"."dom" ORDER BY "id" LIMIT 500'
    verificar(sql, ["s.dom"])


def test_sem_pk_ordena_por_todas_as_colunas_lidas() -> None:
    rel = relacao(restricoes=[])
    lidas, _ = dom.separar_colunas(rel)
    assert dom.ordem_da_consulta(rel, lidas) == ["id", "descricao"]
    pk_omitida = relacao(restricoes=[{"nome": "pk", "tipo": "pk", "colunas": ["anexo"]}])
    assert dom.ordem_da_consulta(pk_omitida, lidas) == ["id", "descricao"]


def test_identificador_com_aspas_e_escapado() -> None:
    sql = dom.montar_sql("s", 'a"b', [{"nome": 'c"d', "tipo": "integer"}], ['c"d'], 5)
    assert '"a""b"' in sql and '"c""d"' in sql


# ----------------------------------------------------------------- valores e máscara


CPF_VALIDO = "529.982.247-25"


def test_valores_sao_cortados_convertidos_e_mascarados() -> None:
    longo = "x" * 500
    linhas = [
        (1, longo, decimal.Decimal("2.50"), datetime.date(2024, 1, 2), True, None),
        (2, f"contato fulano@exemplo.com.br e {CPF_VALIDO}", decimal.Decimal("3"), None, False, "ok"),
    ]
    colunas = ["id", "texto", "valor", "data", "flag", "nulo"]
    saida, masc = dom.tratar_linhas(colunas, linhas)
    assert saida[0] == [1, "x" * 200 + "…", 2.5, "2024-01-02", True, None]
    assert saida[1][1] == "contato [mascarado:email] e [mascarado:cpf]"
    assert saida[1][2] == 3
    assert masc == [{"coluna": "texto", "quantidade": 1, "tipo": "cpf"}, {"coluna": "texto", "quantidade": 1, "tipo": "email"}]
    texto = json.dumps(saida)
    assert "fulano" not in texto and CPF_VALIDO not in texto


def test_numero_que_parece_cpf_tambem_e_mascarado() -> None:
    saida, masc = dom.tratar_linhas(["n"], [(52998224725,), (12345,)])
    assert saida == [["[mascarado:cpf]"], [12345]] and masc[0]["tipo"] == "cpf"


# ----------------------------------------------------------------- execução com banco falso


class BancoFalso:
    """Responde por tabela, olhando o SQL; registra as consultas e a lista liberada de cada uma."""

    def __init__(self, respostas: dict[str, Any]) -> None:
        self.respostas = respostas
        self.consultas: list[str] = []

    def executar_com_colunas(
        self, sql: str, params: Any = None, *, tabelas_liberadas: Collection[str] | None = None
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        verificar(sql, tabelas_liberadas)  # como a Conexao: o guarda vê todo SQL
        self.consultas.append(sql)
        tabela = sql.split(" FROM ")[1].split(" ORDER")[0].replace('"', "")
        resposta = self.respostas[tabela]
        if isinstance(resposta, Exception):
            raise resposta
        return resposta


def estruturas() -> dict[str, dict[str, Any]]:
    return {
        "s": {
            "schema": "s",
            "relacoes": [
                relacao("dom"),
                relacao("outra"),
                relacao("negada", acesso="sem_select"),
                relacao("grande"),
                relacao("falha"),
            ],
        }
    }


def preparar(tmp_path: Path) -> tuple[Path, BancoFalso]:
    doc = tmp_path / "doc"
    doc.mkdir()
    banco = BancoFalso(
        {
            "s.dom": (["id", "descricao"], [(1, "a"), (2, f"b {CPF_VALIDO}")]),
            "s.outra": (["id", "descricao"], [(1, "c")]),
            "s.grande": (["id", "descricao"], [(i, "v") for i in range(5)]),
            "s.falha": ErroExecucao("Erro do banco (sqlstate 42P01): relação inexistente", "42P01"),
        }
    )
    return doc, banco


def rodar_leitura(doc: Path, banco: BancoFalso, aprovadas: set[str], **kw: Any) -> dict[str, Any]:
    pausas: list[float] = []
    kw.setdefault("dormir", pausas.append)
    kw.setdefault("avisar", lambda _: None)
    return dom.ler_dominios(doc, banco, estruturas(), aprovadas, **kw)


def test_leitura_completa_situacoes_indice_e_duvidas(tmp_path: Path) -> None:
    doc, banco = preparar(tmp_path)
    aprovadas = {"s.dom", "s.outra", "s.negada", "s.grande", "s.falha", "s.fantasma"}
    indice = rodar_leitura(doc, banco, aprovadas, limite=5)
    situacoes = {t["tabela"]: t["situacao"] for t in indice["tabelas"]}
    assert situacoes == {
        "s.dom": "lida", "s.outra": "lida", "s.negada": "sem_privilegio",
        "s.grande": "truncada", "s.falha": "erro", "s.fantasma": "erro",
    }  # fmt: skip
    assert indice["resumo"] == {
        "aprovadas": 6, "lida": 2, "truncada": 1, "sem_privilegio": 1, "erro": 2, "nao_lidas": 0, "para_revisao": 1,
    }
    assert not any("s.negada" in c for c in banco.consultas)  # sem SELECT no catálogo: nem consulta
    pasta = doc / "catalogo" / "dominios"
    assert sorted(p.name for p in pasta.iterdir()) == sorted([f"{t}.json" for t in aprovadas] + ["indice.json"])
    dados = json.loads((pasta / "s.dom.json").read_text(encoding="utf-8"))
    assert dados["linhas"] == [[1, "a"], [2, "b [mascarado:cpf]"]] and dados["revisar"] is True
    assert dados["colunas_omitidas"] == [{"nome": "anexo", "tipo": "bytea"}, {"nome": "extra", "tipo": "jsonb"}]
    assert dados["ordenado_por"] == ["id"] and CPF_VALIDO not in json.dumps(dados)
    assert json.loads((pasta / "s.fantasma.json").read_text(encoding="utf-8"))["motivo"] == "tabela fora do catálogo"
    assert "sqlstate 42P01" in json.loads((pasta / "s.falha.json").read_text(encoding="utf-8"))["motivo"]
    duvidas = (doc / "duvidas.md").read_text(encoding="utf-8")
    assert "`s.dom`" in duvidas and "descricao" in duvidas and CPF_VALIDO not in duvidas and "`s.outra`" not in duvidas


def test_so_le_tabelas_aprovadas_e_passa_a_lista_ao_guarda(tmp_path: Path) -> None:
    doc, banco = preparar(tmp_path)
    rodar_leitura(doc, banco, {"s.dom"})
    assert len(banco.consultas) == 1 and '"s"."dom"' in banco.consultas[0]
    # um executor que tentasse outra tabela seria barrado pelo guarda
    with pytest.raises(SqlRejeitado):
        banco.executar_com_colunas('SELECT "id" FROM "s"."outra"', tabelas_liberadas=["s.dom"])


def test_retomada_pula_resolvidas_refaz_erro_e_pausa(tmp_path: Path) -> None:
    doc, banco = preparar(tmp_path)
    rodar_leitura(doc, banco, {"s.dom", "s.falha"})
    assert len(banco.consultas) == 2
    banco.respostas["s.falha"] = (["id", "descricao"], [(9, "ok")])
    pausas: list[float] = []
    indice = rodar_leitura(doc, banco, {"s.dom", "s.falha", "s.outra"}, dormir=pausas.append, pausa_s=0.7)
    assert len(banco.consultas) == 4  # só s.falha (erro) e s.outra; s.dom foi pulada
    assert pausas == [0.7] and indice["resumo"]["lida"] == 3 and indice["resumo"]["erro"] == 0
    rodar_leitura(doc, banco, {"s.dom", "s.falha", "s.outra"}, refazer=True)
    assert len(banco.consultas) == 7


def test_saida_deterministica(tmp_path: Path) -> None:
    doc, banco = preparar(tmp_path)
    aprovadas = {"s.dom", "s.outra"}
    rodar_leitura(doc, banco, aprovadas)
    antes = {p.name: p.read_bytes() for p in (doc / "catalogo" / "dominios").iterdir()}
    rodar_leitura(doc, banco, aprovadas, refazer=True)
    assert {p.name: p.read_bytes() for p in (doc / "catalogo" / "dominios").iterdir()} == antes
    assert all(b"\r\n" not in b for b in antes.values())


def test_perda_de_conexao_grava_indice_parcial(tmp_path: Path) -> None:
    doc, banco = preparar(tmp_path)
    banco.respostas["s.outra"] = ErroConexao("caiu")
    with pytest.raises(ErroConexao):
        rodar_leitura(doc, banco, {"s.dom", "s.outra"})
    indice = json.loads((doc / "catalogo" / "dominios" / "indice.json").read_text(encoding="utf-8"))
    assert [t["tabela"] for t in indice["tabelas"]] == ["s.dom"] and indice["resumo"]["nao_lidas"] == 1


def test_sem_privilegio_no_servidor(tmp_path: Path) -> None:
    doc, banco = preparar(tmp_path)
    banco.respostas["s.dom"] = ErroExecucao("Erro do banco (sqlstate 42501): negado", "42501")
    indice = rodar_leitura(doc, banco, {"s.dom"})
    assert indice["tabelas"][0]["situacao"] == "sem_privilegio"


def test_nome_invalido_na_configuracao(tmp_path: Path) -> None:
    doc, banco = preparar(tmp_path)
    with pytest.raises(dom.ErroDominios):
        rodar_leitura(doc, banco, {"semschema"})


def test_duvidas_preserva_texto_e_remove_secao_quando_nao_ha_revisao(tmp_path: Path) -> None:
    doc, banco = preparar(tmp_path)
    (doc / "duvidas.md").write_text("# Dúvidas\n\n## Outra seção\n\n- item\n", encoding="utf-8")
    rodar_leitura(doc, banco, {"s.dom"})
    texto = (doc / "duvidas.md").read_text(encoding="utf-8")
    assert "## Outra seção" in texto and dom.MARCA_INICIO in texto
    banco.respostas["s.dom"] = (["id", "descricao"], [(1, "a")])
    rodar_leitura(doc, banco, {"s.dom"}, refazer=True)
    assert (doc / "duvidas.md").read_text(encoding="utf-8") == "# Dúvidas\n\n## Outra seção\n\n- item\n"


# ----------------------------------------------------------------- pacote de contexto


def gravar_resultado(doc: Path, tabela: str, linhas: int, **extra: Any) -> None:
    ok = dom._resultado(
        tabela, "lida", colunas=["id", "descricao"], ordem=["id"],
        linhas=[[i, f"v{i}"] for i in range(linhas)], **extra,
    )  # fmt: skip
    escrever_json(doc / "catalogo" / "dominios" / f"{tabela}.json", ok)


def test_pacote_pendente_sem_arquivo_e_com_valores_apos_leitura(tmp_path: Path) -> None:
    doc = montar_doc(tmp_path)
    assert rodar(doc) == 0
    pacote = json.loads((doc / "_controle" / "lotes" / "lote-001.json").read_text(encoding="utf-8"))
    assert pacote["dominios"] == [{"situacao": "pendente", "tabela": "s.dom", "valores": None}]
    gravar_resultado(doc, "s.dom", 3)
    assert rodar(doc) == 0
    pacote = json.loads((doc / "_controle" / "lotes" / "lote-001.json").read_text(encoding="utf-8"))
    assert pacote["dominios"] == [
        {"motivo": None, "situacao": "lida", "tabela": "s.dom", "total_linhas": 3,
         "valores": {"colunas": ["id", "descricao"], "linhas": [[0, "v0"], [1, "v1"], [2, "v2"]]}}
    ]  # fmt: skip
    indice = json.loads((doc / "_controle" / "lotes" / "contexto-indice.json").read_text(encoding="utf-8"))
    assert indice["resumo"]["dominios_pendentes"] == 0 and indice["resumo"]["dominios_por_situacao"] == {"lida": 1}


def test_pacote_corta_em_100_linhas_e_aponta_o_arquivo(tmp_path: Path) -> None:
    doc = montar_doc(tmp_path)
    gravar_resultado(doc, "s.dom", 130)
    rodar(doc)
    d = json.loads((doc / "_controle" / "lotes" / "lote-001.json").read_text(encoding="utf-8"))["dominios"][0]
    assert len(d["valores"]["linhas"]) == 100 and d["linhas_omitidas_no_pacote"] == 30
    assert d["arquivo"] == "catalogo/dominios/s.dom.json" and d["total_linhas"] == 130


def test_pacote_leva_situacoes_sem_valores(tmp_path: Path) -> None:
    doc = montar_doc(tmp_path, aprovadas=["s.dom"])
    escrever_json(
        doc / "catalogo" / "dominios" / "s.dom.json",
        dom._resultado("s.dom", "sem_privilegio", motivo="sem SELECT segundo o catálogo", omitidas=[{"nome": "x", "tipo": "bytea"}]),
    )
    rodar(doc)
    d = json.loads((doc / "_controle" / "lotes" / "lote-001.json").read_text(encoding="utf-8"))["dominios"][0]
    assert d["situacao"] == "sem_privilegio" and d["valores"] is None and d["motivo"].startswith("sem SELECT")
    assert d["colunas_omitidas"] == ["x"]


def test_arquivo_de_dominio_invalido_ou_de_tabela_nao_aprovada_e_ignorado(tmp_path: Path) -> None:
    doc = montar_doc(tmp_path)
    (doc / "catalogo" / "dominios").mkdir(parents=True)
    (doc / "catalogo" / "dominios" / "s.dom.json").write_text("{quebrado", encoding="utf-8")
    gravar_resultado(doc, "s.t1", 2)  # t1 não está em dominios.yml
    rodar(doc)
    pacote = json.loads((doc / "_controle" / "lotes" / "lote-001.json").read_text(encoding="utf-8"))
    assert pacote["dominios"][0]["situacao"] == "pendente"
    assert "\"v1\"" not in json.dumps(pacote["dominios"])


# ----------------------------------------------------------------- banco real (opcional)


@pytest.mark.banco
def test_dominios_ler_no_banco_real() -> None:
    """Lê os valores de verdade (exige `PGATLAS_DOC`); comando: `-m banco -k dominios_ler`."""
    import os

    from pg_atlas import cli

    doc = os.environ.get("PGATLAS_DOC")
    if not doc:
        pytest.skip("defina PGATLAS_DOC com o caminho do repositório de documentação")
    assert cli.main(["dominios", "ler", "--doc", doc]) == 0
