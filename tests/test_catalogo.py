"""Testes offline do extrator do catálogo (tarefa 1.2). Nomes e dados são fictícios."""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from pg_atlas import catalogo, catalogo_montagem as m, catalogo_sql, cli, mascara
from pg_atlas.config import Projeto
from pg_atlas.erros import ErroConfig


def linhas_exemplo() -> m.Linhas:
    """Linhas simuladas de um schema fictício, propositalmente fora de ordem."""
    return {
        "schemas": [{"schema": "loja", "dono": "dono_a", "comentario": "Schema de teste"}],
        "relacoes": [
            {"schema": "loja", "nome": "venda", "tipo": "r", "dono": "dono_a",
             "persistencia": "p", "comentario": None, "eh_particao": False,
             "pai_schema": None, "pai_nome": None, "limite_particao": None,
             "chave_particao": None},
            {"schema": "loja", "nome": "cliente", "tipo": "p", "dono": "dono_a",
             "persistencia": "p", "comentario": "Clientes", "eh_particao": False,
             "pai_schema": None, "pai_nome": None, "limite_particao": None,
             "chave_particao": "RANGE (id)"},
            {"schema": "loja", "nome": "cliente_p1", "tipo": "r", "dono": "dono_a",
             "persistencia": "p", "comentario": None, "eh_particao": True,
             "pai_schema": "loja", "pai_nome": "cliente",
             "limite_particao": "FOR VALUES FROM (1) TO (10)", "chave_particao": None},
            {"schema": "loja", "nome": "v_venda", "tipo": "v", "dono": "dono_a",
             "persistencia": "p", "comentario": None, "eh_particao": False,
             "pai_schema": None, "pai_nome": None, "limite_particao": None,
             "chave_particao": None},
            {"schema": "loja", "nome": "mv_venda", "tipo": "m", "dono": "dono_a",
             "persistencia": "p", "comentario": None, "eh_particao": False,
             "pai_schema": None, "pai_nome": None, "limite_particao": None,
             "chave_particao": None},
        ],
        "colunas": [
            {"schema": "loja", "relacao": "venda", "ordem": 2, "nome": "valor",
             "tipo": "numeric(10,2)", "obrigatoria": False, "padrao": None, "identidade": "",
             "gerada": "", "expressao_gerada": None, "comentario": None},
            {"schema": "loja", "relacao": "venda", "ordem": 1, "nome": "id", "tipo": "integer",
             "obrigatoria": True, "padrao": None, "identidade": "d", "gerada": "",
             "expressao_gerada": None, "comentario": "Chave"},
        ],
        "restricoes": [
            {"schema": "loja", "relacao": "venda", "nome": "venda_pk", "tipo": "p",
             "colunas": ["id"], "ref_schema": None, "ref_relacao": None, "ref_colunas": None,
             "acao_delete": None, "acao_update": None, "tipo_match": None, "adiavel": False,
             "adiada": False, "validada": True, "herdada": False,
             "definicao": "PRIMARY KEY (id)"},
            {"schema": "loja", "relacao": "venda", "nome": "venda_cliente_fk", "tipo": "f",
             "colunas": ["id"], "ref_schema": "loja", "ref_relacao": "cliente",
             "ref_colunas": ["id"], "acao_delete": "c", "acao_update": "a", "tipo_match": "s",
             "adiavel": False, "adiada": False, "validada": True, "herdada": False,
             "definicao": "FOREIGN KEY (id) REFERENCES loja.cliente(id) ON DELETE CASCADE"},
        ],
        "indices": [
            {"schema": "loja", "relacao": "venda", "nome": "venda_pk", "metodo": "btree",
             "colunas": ["id"], "unico": True, "primario": True, "parcial": False,
             "predicado": None, "valido": True, "definicao": "CREATE UNIQUE INDEX ..."},
        ],
        "views": [
            {"schema": "loja", "nome": "v_venda", "tipo": "v", "dono": "dono_a",
             "populada": None, "definicao": "SELECT id FROM loja.venda"},
            {"schema": "loja", "nome": "mv_venda", "tipo": "m", "dono": "dono_a",
             "populada": True, "definicao": "SELECT id FROM loja.venda"},
        ],
        "funcoes": [
            {"schema": "loja", "nome": "somar", "tipo": "function", "linguagem": "sql",
             "argumentos": "a integer, b integer", "argumentos_identidade": "a integer, b integer",
             "retorno": "integer", "volatilidade": "i", "security_definer": False,
             "estrita": True, "dono": "dono_a", "comentario": None, "codigo": "CREATE ..."},
            {"schema": "loja", "nome": "agregar", "tipo": "aggregate", "linguagem": "internal",
             "argumentos": "integer", "argumentos_identidade": "integer", "retorno": "integer",
             "volatilidade": "i", "security_definer": False, "estrita": False,
             "dono": "dono_a", "comentario": None, "codigo": None},
        ],
        "gatilhos": [
            {"schema": "loja", "relacao": "venda", "nome": "trg_b", "eventos": ["UPDATE", "INSERT"],
             "momento": "BEFORE", "nivel": "ROW", "habilitado": "O", "funcao_schema": "loja",
             "funcao_nome": "somar", "definicao": "CREATE TRIGGER ..."},
        ],
        "sequencias": [
            {"schema": "loja", "nome": "venda_id_seq", "tipo": "integer", "inicio": 1, "minimo": 1,
             "maximo": 2147483647, "incremento": 1, "ciclica": False, "cache": 1,
             "dono": "dono_a", "coluna_schema": "loja", "coluna_relacao": "venda",
             "coluna_nome": "id", "por_identidade": True},
        ],
        "tipos": [
            {"schema": "loja", "nome": "situacao", "categoria": "enum", "dono": "dono_a",
             "comentario": None, "tipo_base": None, "obrigatorio": None, "padrao": None,
             "restricoes": None, "valores": ["novo", "pago"], "atributos": None},
        ],
        "dependencias": [
            {"schema": "loja", "view": "v_venda", "tipo_view": "v", "ref_schema": "loja",
             "ref_relacao": "venda", "ref_tipo": "r"},
            {"schema": "loja", "view": "v_venda", "tipo_view": "v", "ref_schema": "base",
             "ref_relacao": "outra", "ref_tipo": "r"},
        ],
        "privilegios": [
            {"schema": "loja", "classe": "relacao", "nome": "venda", "tipo": "r",
             "argumentos": None, "tem_select": False, "tem_execute": None, "acesso": "sem_select"},
            {"schema": "loja", "classe": "relacao", "nome": "cliente", "tipo": "p",
             "argumentos": None, "tem_select": True, "tem_execute": None, "acesso": "ok"},
            {"schema": "loja", "classe": "funcao", "nome": "somar", "tipo": "f",
             "argumentos": "a integer, b integer", "tem_select": None, "tem_execute": False,
             "acesso": "ok"},
        ],
        "estatisticas": [
            {"schema": "loja", "relacao": "venda", "tipo": "r", "linhas_estimadas": 100,
             "paginas": 5, "tamanho_total_bytes": 4096, "tamanho_tabela_bytes": 2048,
             "ultimo_analyze": datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
             "ultimo_autoanalyze": None, "ultimo_vacuum": None, "ultimo_autovacuum": None,
             "seq_scans": 3, "linhas_lidas_seq": 10, "idx_scans": 7, "linhas_vivas": 99,
             "linhas_mortas": 1},
        ],
        "estatisticas_colunas": [
            {"schemaname": "loja", "tablename": "venda", "attname": "valor", "inherited": False,
             "null_frac": 0.1, "n_distinct": Decimal("-1"), "avg_width": 8, "correlation": 0.5},
            {"schemaname": "loja", "tablename": "venda", "attname": "id", "inherited": False,
             "null_frac": 0.0, "n_distinct": -1.0, "avg_width": 4, "correlation": None},
            {"schemaname": "loja", "tablename": "indice_expr", "attname": "expr",
             "inherited": False, "null_frac": 0.0, "n_distinct": 1.0, "avg_width": 4,
             "correlation": None},
        ],
    }


# ----------------------------------------------------------------- montagem


def test_estrutura_ordenada_e_valida() -> None:
    est = m.montar_estrutura("loja", linhas_exemplo())
    m.validar(est)
    assert [r["nome"] for r in est["relacoes"]] == [
        "cliente", "cliente_p1", "mv_venda", "v_venda", "venda"
    ]
    venda = next(r for r in est["relacoes"] if r["nome"] == "venda")
    assert [c["nome"] for c in venda["colunas"]] == ["id", "valor"]
    assert venda["colunas"][0]["identidade"] == "padrao"
    assert [r["nome"] for r in venda["restricoes"]] == ["venda_cliente_fk", "venda_pk"]
    fk = venda["restricoes"][0]
    assert (fk["tipo"], fk["acao_delete"], fk["acao_update"]) == ("fk", "cascade", "no_action")
    assert venda["gatilhos"][0]["eventos"] == ["INSERT", "UPDATE"]
    assert [f["nome"] for f in est["funcoes"]] == ["agregar", "somar"]


def test_acesso_particao_views_e_dependencias() -> None:
    est = m.montar_estrutura("loja", linhas_exemplo())
    por_nome = {r["nome"]: r for r in est["relacoes"]}
    assert por_nome["venda"]["acesso"] == "sem_select"
    assert por_nome["cliente"]["acesso"] == "ok"
    assert por_nome["cliente_p1"]["particao"]["pai_nome"] == "cliente"
    assert por_nome["mv_venda"]["populada"] is True
    assert por_nome["v_venda"]["definicao"].startswith("SELECT")
    assert [(d["schema"], d["relacao"]) for d in por_nome["v_venda"]["depende_de"]] == [
        ("base", "outra"), ("loja", "venda")
    ]
    funcoes = {f["nome"]: f for f in est["funcoes"]}
    assert funcoes["somar"]["acesso"] == "sem_execute"
    assert funcoes["agregar"]["codigo"] is None


def test_estrutura_nao_contem_valores_volateis() -> None:
    texto = json.dumps(m.montar_estrutura("loja", linhas_exemplo()))
    for proibido in ("linhas_estimadas", "tamanho_total", "ultimo_analyze", "seq_scans",
                     "null_frac", "n_distinct", "correlation", "extraido_em"):  # fmt: skip
        assert proibido not in texto


def test_ordem_das_linhas_de_entrada_nao_altera_a_saida() -> None:
    normal = linhas_exemplo()
    invertido = {k: list(reversed(v)) for k, v in copy.deepcopy(normal).items()}
    assert m.montar_estrutura("loja", normal) == m.montar_estrutura("loja", invertido)
    assert m.montar_estatisticas("loja", normal, "t") == m.montar_estatisticas(
        "loja", invertido, "t"
    )


def test_estatisticas_so_campos_permitidos_e_json_seguro() -> None:
    est = m.montar_estatisticas("loja", linhas_exemplo(), "2026-01-01T00:00:00+00:00")
    m.validar(est)
    json.dumps(est)  # tudo serializável
    venda = est["relacoes"][0]
    assert venda["ultimo_analyze"] == "2026-01-02T03:04:05+00:00"
    assert [c["nome"] for c in venda["colunas"]] == ["id", "valor"]
    assert set(venda["colunas"][0]) == {"nome", "herdada", "null_frac", "n_distinct",
                                         "avg_width", "correlation"}  # fmt: skip
    assert venda["colunas"][1]["n_distinct"] == -1  # Decimal convertido
    assert "indice_expr" not in json.dumps(est)


def test_contagens_e_banco() -> None:
    est = m.montar_estrutura("loja", linhas_exemplo())
    c = m.contar_objetos(est)
    assert (c["tabela"], c["particionada"], c["view"], c["materializada"]) == (2, 1, 1, 1)
    assert (c["colunas"], c["funcoes"], c["sem_select"], c["tipos"]) == (2, 2, 1, 1)
    banco = m.montar_banco("PostgreSQL 99", 123, "2026-01-01T00:00:00+00:00", {"loja": c})
    m.validar(banco)
    assert banco["totais"]["tabela"] == 2 and banco["total_schemas"] == 1


# ----------------------------------------------------------------- validação


def test_validacao_aponta_o_caminho_do_erro() -> None:
    est = m.montar_estrutura("loja", linhas_exemplo())
    est["relacoes"][0]["tipo"] = "inventado"
    with pytest.raises(m.ErroValidacaoCatalogo, match=r"relacoes/0/tipo"):
        m.validar(est, "loja.json")


def test_validacao_rejeita_chave_extra_e_formato_ausente() -> None:
    est = m.montar_estrutura("loja", linhas_exemplo())
    est["extra"] = 1
    with pytest.raises(m.ErroValidacaoCatalogo):
        m.validar(est)
    with pytest.raises(m.ErroValidacaoCatalogo, match="formato"):
        m.validar({"schema": "x"})


def test_esquema_e_json_valido_e_empacotado() -> None:
    esquema = m.carregar_esquema()
    assert set(esquema["$defs"]) >= set(m.FORMATOS)


# ----------------------------------------------------------------- fluxo com fonte simulada


class FonteFalsa:
    """Imita `Conexao.executar_com_colunas` com dados fictícios, registrando as consultas."""

    def __init__(self, schemas: dict[str, m.Linhas], falhar_em: str | None = None) -> None:
        self.schemas = schemas
        self.falhar_em = falhar_em
        self.chamadas: list[tuple[str, str | None]] = []
        self._textos = {catalogo_sql.carregar(n): n for n in catalogo_sql.listar()}

    def executar_com_colunas(self, sql: str, params: Any = None):
        if sql == catalogo.SQL_VERSAO:
            return ["version"], [("PostgreSQL 99 (teste)",)]
        if sql == catalogo.SQL_TAMANHO:
            return ["pg_database_size"], [(1000,)]
        nome = self._textos[sql]
        schema = params["schema"]
        self.chamadas.append((nome, schema))
        if schema is not None and schema == self.falhar_em and nome == "indices":
            raise RuntimeError("falha simulada")
        if nome == "schemas" and schema is None:
            linhas = [{"schema": s, "dono": "d", "comentario": None} for s in self.schemas]
        else:
            linhas = self.schemas[schema].get(nome, [])
        if not linhas:
            return ["schema"], []
        colunas = list(linhas[0])
        return colunas, [tuple(l.get(c) for c in colunas) for l in linhas]


def fonte_com(nomes: list[str]) -> FonteFalsa:
    dados = {}
    for n in nomes:
        base = linhas_exemplo()
        for lista in base.values():
            for l in lista:
                if "schema" in l:
                    l["schema"] = n
                if "schemaname" in l:
                    l["schemaname"] = n
        base["schemas"][0]["schema"] = n
        dados[n] = base
    return FonteFalsa(dados)


@pytest.fixture
def projeto(tmp_path: Path) -> Projeto:
    doc = tmp_path / "doc"
    (doc / "_controle").mkdir(parents=True)
    (doc / ".gitignore").write_text("_controle/mascaramento-mapa.local.json\n", encoding="utf-8")
    estado = {"fases": {"1": {"status": "em_andamento", "tarefas": {
        "1.2": {"descricao": "Extrator", "status": "pendente"}}}}}  # fmt: skip
    (doc / "_controle" / "estado.json").write_text(json.dumps(estado), encoding="utf-8")
    return Projeto(doc=doc, workspace=tmp_path, dados={"escopo": {"schemas": "todos", "excluir": []}})


def relogio() -> str:
    return "2026-02-03T04:05:06+00:00"


def estado_da(projeto: Projeto) -> dict[str, Any]:
    return json.loads((projeto.controle / "estado.json").read_text(encoding="utf-8"))


def tarefa(projeto: Projeto) -> dict[str, Any]:
    return estado_da(projeto)["fases"]["1"]["tarefas"]["1.2"]


def test_extracao_completa_grava_arquivos_e_progresso(projeto: Projeto) -> None:
    fonte = fonte_com(["zeta", "alfa"])
    resumo = catalogo.extrair(projeto, fonte, relogio=relogio, imprimir=lambda _: None)
    raiz = projeto.doc / "catalogo"
    assert sorted(p.name for p in (raiz / "estrutura").iterdir()) == ["alfa.json", "zeta.json"]
    assert sorted(p.name for p in (raiz / "estatisticas").iterdir()) == ["alfa.json", "zeta.json"]
    banco = json.loads((raiz / "_banco.json").read_text(encoding="utf-8"))
    assert banco["total_schemas"] == 2 and banco["versao"].startswith("PostgreSQL")
    assert list(banco["schemas"]) == ["alfa", "zeta"]
    t = tarefa(projeto)
    assert t["status"] == "pendente"  # o extrator nunca muda o status
    assert t["progresso"] == {"total": 2, "concluidos": 2}
    assert t["schemas"] == {"alfa": "concluido", "zeta": "concluido"}
    assert resumo["processados"] == ["alfa", "zeta"]
    # Ordem alfabética de processamento.
    ordem = [s for n, s in fonte.chamadas if n == "relacoes"]
    assert ordem == ["alfa", "zeta"]


def test_arquivos_terminam_em_nova_linha_e_sao_deterministicos(projeto: Projeto) -> None:
    catalogo.extrair(projeto, fonte_com(["alfa"]), relogio=relogio, imprimir=lambda _: None)
    arq = projeto.doc / "catalogo" / "estrutura" / "alfa.json"
    primeiro = arq.read_bytes()
    assert primeiro.endswith(b"\n") and b"\r" not in primeiro
    catalogo.extrair(projeto, fonte_com(["alfa"]), relogio=lambda: "outra hora",
                     imprimir=lambda _: None)  # fmt: skip
    assert arq.read_bytes() == primeiro  # estrutura não muda com a data


def test_schema_unico_preserva_os_demais(projeto: Projeto) -> None:
    catalogo.extrair(projeto, fonte_com(["alfa", "beta"]), relogio=relogio, imprimir=lambda _: None)
    fonte = fonte_com(["alfa", "beta"])
    resumo = catalogo.extrair(projeto, fonte, schema="beta", relogio=relogio,
                              imprimir=lambda _: None)  # fmt: skip
    assert resumo["processados"] == ["beta"]
    assert {s for n, s in fonte.chamadas if n == "relacoes"} == {"beta"}
    banco = json.loads((projeto.doc / "catalogo" / "_banco.json").read_text(encoding="utf-8"))
    assert list(banco["schemas"]) == ["alfa", "beta"]
    assert tarefa(projeto)["progresso"] == {"total": 2, "concluidos": 2}


def test_schema_inexistente_falha_com_mensagem(projeto: Projeto) -> None:
    with pytest.raises(ErroConfig, match="fora do escopo|não existe"):
        catalogo.extrair(projeto, fonte_com(["alfa"]), schema="nada", imprimir=lambda _: None)


def test_falha_marca_erro_e_retomar_continua(projeto: Projeto) -> None:
    fonte = fonte_com(["a1", "b2", "c3"])
    fonte.falhar_em = "b2"
    with pytest.raises(RuntimeError):
        catalogo.extrair(projeto, fonte, relogio=relogio, imprimir=lambda _: None)
    t = tarefa(projeto)
    assert t["schemas"] == {"a1": "concluido", "b2": "erro", "c3": "pendente"}
    assert t["progresso"] == {"total": 3, "concluidos": 1}
    fonte2 = fonte_com(["a1", "b2", "c3"])
    resumo = catalogo.extrair(projeto, fonte2, retomar=True, relogio=relogio,
                              imprimir=lambda _: None)  # fmt: skip
    assert resumo["pulados"] == ["a1"] and resumo["processados"] == ["b2", "c3"]
    assert {s for n, s in fonte2.chamadas if n == "relacoes"} == {"b2", "c3"}
    assert tarefa(projeto)["progresso"] == {"total": 3, "concluidos": 3}
    banco = json.loads((projeto.doc / "catalogo" / "_banco.json").read_text(encoding="utf-8"))
    assert banco["total_schemas"] == 3  # contagem do schema pulado preservada


def test_sem_retomar_recomeca_do_zero(projeto: Projeto) -> None:
    catalogo.extrair(projeto, fonte_com(["a1", "b2"]), relogio=relogio, imprimir=lambda _: None)
    fonte = fonte_com(["a1", "b2"])
    catalogo.extrair(projeto, fonte, relogio=relogio, imprimir=lambda _: None)
    assert {s for n, s in fonte.chamadas if n == "relacoes"} == {"a1", "b2"}


def test_escopo_incluir_e_excluir(projeto: Projeto) -> None:
    proj = Projeto(doc=projeto.doc, workspace=projeto.workspace,
                   dados={"escopo": {"schemas": "todos", "excluir": ["b2"]}})  # fmt: skip
    assert catalogo.listar_schemas(fonte_com(["a1", "b2", "c3"]), proj) == ["a1", "c3"]
    proj = Projeto(doc=projeto.doc, workspace=projeto.workspace,
                   dados={"escopo": {"schemas": ["c3", "zz"]}})  # fmt: skip
    assert catalogo.listar_schemas(fonte_com(["a1", "c3"]), proj) == ["c3"]


def test_estado_preserva_campos_existentes(projeto: Projeto) -> None:
    catalogo.gravar_progresso(projeto.controle / "estado.json", {"x": "concluido", "y": "pendente"})
    t = tarefa(projeto)
    assert t["descricao"] == "Extrator" and t["progresso"] == {"total": 2, "concluidos": 1}
    assert estado_da(projeto)["fases"]["1"]["status"] == "em_andamento"


def test_planejar() -> None:
    todos = ["a", "b", "c"]
    mapa, proc = catalogo.planejar({"a": "concluido", "b": "erro"}, todos, todos, True)
    assert mapa == {"a": "concluido", "b": "pendente", "c": "pendente"} and proc == ["b", "c"]
    mapa, proc = catalogo.planejar({"a": "concluido"}, todos, todos, False)
    assert set(mapa.values()) == {"pendente"} and proc == todos
    mapa, proc = catalogo.planejar({"a": "concluido", "b": "concluido"}, todos, ["b"], False)
    assert mapa["a"] == "concluido" and mapa["b"] == "pendente" and proc == ["b"]
    mapa, proc = catalogo.planejar({"b": "concluido"}, todos, ["b"], True)
    assert proc == []


def test_so_consultas_de_catalogo_sao_usadas(projeto: Projeto) -> None:
    fonte = fonte_com(["alfa"])
    catalogo.extrair(projeto, fonte, relogio=relogio, imprimir=lambda _: None)
    assert {n for n, _ in fonte.chamadas} == set(catalogo_sql.listar())


def test_comando_registrado_na_cli() -> None:
    assert "pg_atlas.catalogo" in cli.MODULOS_DE_COMANDOS
    args = cli.criar_parser().parse_args(["catalogo", "--doc", "x", "--schema", "s", "--retomar"])
    assert args.schema == "s" and args.retomar is True and args.func is catalogo.executar


# ----------------------------------------------------------------- banco real (opcional)


@pytest.mark.banco
def test_banco_um_schema(tmp_path: Path) -> None:
    """Extrai um schema do banco real para uma pasta temporária (exige `PGATLAS_DOC`)."""
    doc = os.environ.get("PGATLAS_DOC")
    if not doc:
        pytest.skip("defina PGATLAS_DOC com o caminho do repositório de documentação")
    from pg_atlas.config import carregar_projeto
    from pg_atlas.conexao import Conexao

    real = carregar_projeto(doc)
    saida = Projeto(doc=tmp_path / "doc", workspace=real.workspace, dados=real.dados)
    (tmp_path / "doc" / "_controle").mkdir(parents=True)
    with Conexao(real) as conexao:
        primeiro = catalogo.listar_schemas(conexao, real)[0]
        resumo = catalogo.extrair(saida, conexao, schema=primeiro, imprimir=lambda _: None)
    assert resumo["processados"] == [primeiro]
    assert (tmp_path / "doc" / "catalogo" / "estrutura" / f"{primeiro}.json").is_file()


# ----------------------------------------------------------------- máscara (D-006)


def _cpf(base: str) -> str:
    d = [int(c) for c in base]
    for tam in (9, 10):
        d.append(mascara._dv(d[:tam], list(range(tam + 1, 1, -1))))
    return "".join(map(str, d))


def _cnpj(base: str) -> str:
    d = [int(c) for c in base]
    p1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    d.append(mascara._dv(d, p1))
    d.append(mascara._dv(d, [6] + p1))
    return "".join(map(str, d))


def _formatar_cpf(c: str) -> str:
    return f"{c[:3]}.{c[3:6]}.{c[6:9]}-{c[9:]}"


def _formatar_cnpj(c: str) -> str:
    return f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}"


def test_mascara_cpf_cnpj_validos() -> None:
    cpf, cnpj = _cpf("123456789"), _cnpj("112223330001")
    assert mascara.cpf_valido(cpf) and mascara.cnpj_valido(cnpj)
    texto = f"a {_formatar_cpf(cpf)} b {cpf} c {_formatar_cnpj(cnpj)} d {cnpj}"
    novo, cont = mascara.mascarar(texto)
    assert novo == "a [mascarado:cpf:1] b [mascarado:cpf:1] c [mascarado:cnpj:1] d [mascarado:cnpj:1]"
    assert cont == {"cnpj": 2, "cpf": 2}
    assert mascara.encontrar(novo) == {}  # idempotente


def test_mascara_email_e_telefone() -> None:
    texto = "x fulano.teste@exemplo.com.br y (69) 3333-4444, (69) 99999-8888, +55 69 99999-8888, 69 3333-4444"
    novo, cont = mascara.mascarar(texto)
    assert cont == {"email": 1, "telefone": 4}
    assert "@" not in novo and "3333" not in novo and "9999" not in novo


@pytest.mark.parametrize(
    "texto",
    [
        "12345678901",  # DV inválido
        "123.456.789-00",
        "00000000000",  # repetido: DV "válido" mas não é dado
        "11111111111",
        "00000000000000",
        "12345678000195000",  # número longo
        "550e8400-e29b-41d4-a716-446655440000",
        "2026-01-02 03:04:05",
        "123456789012345",  # 15 dígitos
        "x_123456789012345_y",
        "proc_" + "1" * 14,
        "SELECT a @> b FROM t",
        "nextval('seq'::regclass) 12345678",
        "1234-5678",
    ],
)
def test_mascara_negativos(texto: str) -> None:
    assert mascara.mascarar(texto) == (texto, {})


def test_mascarar_estrutura_registra_sem_valores() -> None:
    linhas = linhas_exemplo()
    cpf = _formatar_cpf(_cpf("987654321"))
    linhas["views"][0]["definicao"] = f"SELECT '{cpf}' AS c, 'a@b.com' AS e FROM loja.venda"
    linhas["colunas"][0]["padrao"] = "'x@y.org'::text"
    linhas["tipos"][0]["valores"] = ["novo", cpf]
    est, regs = mascara.mascarar_estrutura(m.montar_estrutura("loja", linhas))
    m.validar(est)
    assert mascara.varrer_documento(est) == []
    assert cpf not in json.dumps(est) and cpf not in json.dumps(regs)
    chaves = [(r["objeto"], r["campo"], r["tipo"], r["quantidade"]) for r in regs]
    assert chaves == sorted(chaves)
    assert ("relacao:v_venda", "definicao", "cpf", 1) in chaves
    assert ("relacao:venda", "colunas[valor].padrao", "email", 1) in chaves
    assert ("tipo:situacao", "valores[1]", "cpf", 1) in chaves
    assert all(r["schema"] == "loja" for r in regs)


def test_extracao_mascara_e_preserva_registros_de_outros_schemas(projeto: Projeto) -> None:
    fonte = fonte_com(["alfa", "beta"])
    cpf = _formatar_cpf(_cpf("111222333"))
    for nome in ("alfa", "beta"):
        fonte.schemas[nome]["views"][0]["definicao"] = f"SELECT '{cpf}'"
    catalogo.extrair(projeto, fonte, relogio=relogio, imprimir=lambda _: None)
    arq = projeto.controle / "mascaramento.json"
    doc = json.loads(arq.read_text(encoding="utf-8"))
    assert [r["schema"] for r in doc["registros"]] == ["alfa", "beta"]
    assert cpf not in arq.read_text(encoding="utf-8")
    assert catalogo.verificar_mascara(projeto.doc / "catalogo", lambda _: None) == 0
    fonte2 = fonte_com(["alfa", "beta"])
    catalogo.extrair(projeto, fonte2, schema="beta", relogio=relogio, imprimir=lambda _: None)
    doc2 = json.loads(arq.read_text(encoding="utf-8"))
    assert [r["schema"] for r in doc2["registros"]] == ["alfa"]  # beta agora sem ocorrências


def test_verificador_acusa_sem_imprimir_valor(tmp_path: Path) -> None:
    cnpj = _formatar_cnpj(_cnpj("445556660001"))
    pasta = tmp_path / "catalogo" / "estrutura"
    pasta.mkdir(parents=True)
    (pasta / "s.json").write_text(json.dumps({"relacoes": [{"nome": "t", "definicao": cnpj}]}))
    saida: list[str] = []
    assert catalogo.verificar_mascara(tmp_path / "catalogo", saida.append) == 1
    assert saida == ["estrutura/s.json: relacoes[t].definicao: cnpj x1"]
    assert cnpj not in "".join(saida)


def test_esquema_enumera_vocabulario() -> None:
    defs = m.carregar_esquema()["$defs"]
    for tipo, campo in [("restricao", "tipo"), ("restricao", "acao_delete"), ("funcao", "volatilidade"),
                        ("coluna", "identidade"), ("relacao", "tipo"), ("relacao", "acesso"),
                        ("relacao", "persistencia"), ("gatilho", "momento"), ("gatilho", "habilitado"),
                        ("funcao", "acesso"), ("tipo", "categoria"), ("dependencia", "tipo")]:  # fmt: skip
        assert "enum" in defs[tipo]["properties"][campo], (tipo, campo)


# ----------------------------------------------------------------- D-009: identificadores


def test_mascara_colada_a_letras_e_underscore() -> None:
    cnpj, cpf = _cnpj("556677880001"), _cpf("246813579")
    for texto in (f"proc_a_{cnpj}", f"{cnpj}_x", f"abc{cpf}def", f"fn_{_formatar_cnpj(cnpj)}"):
        novo, cont = mascara.mascarar(texto)
        assert cont and cnpj not in novo and cpf not in novo and mascara.encontrar(novo) == {}
    assert mascara.mascarar(f"proc_{cnpj}9")[1] == {}  # 15 dígitos
    invalido = cnpj[:-1] + str((int(cnpj[-1]) + 1) % 10)
    assert mascara.mascarar(f"proc_{invalido}")[1] == {}


def test_marcador_numerado_e_nome_de_arquivo() -> None:
    a, b = _cnpj("100200300001"), _cnpj("100200300002")
    masc = mascara.Mascarador()
    t1, _ = masc.mascarar(f"p_{a} p_{b} p_{a} {_formatar_cnpj(a)}")
    assert t1 == "p_[mascarado:cnpj:1] p_[mascarado:cnpj:2] p_[mascarado:cnpj:1] [mascarado:cnpj:1]"
    assert mascara.nome_de_arquivo("proc_x_[mascarado:cnpj:12]") == "proc_x_mascarado-cnpj-12"
    assert mascara.nome_de_arquivo("[mascarado:email]") == "mascarado-email"
    assert ":" not in mascara.nome_de_arquivo(t1)


def _linhas_com_nome_sensivel(cnpj: str, outro: str) -> m.Linhas:
    linhas = linhas_exemplo()
    linhas["funcoes"][0]["nome"] = f"proc_a_{cnpj}"
    linhas["funcoes"][0]["codigo"] = f"CREATE FUNCTION loja.proc_a_{cnpj}() ... PERFORM loja.proc_a_{cnpj}()"
    linhas["funcoes"][1]["nome"] = f"proc_a_{outro}"
    linhas["relacoes"][0]["nome"] = f"tab_{cnpj}"
    linhas["colunas"][0]["relacao"] = f"tab_{cnpj}"
    linhas["privilegios"][0]["nome"] = f"tab_{cnpj}"
    linhas["restricoes"][1]["ref_relacao"] = f"tab_{cnpj}"
    return linhas


def test_mesmo_valor_mesmo_n_e_valores_distintos_sem_duplicata() -> None:
    cnpj, outro = _cnpj("300400500001"), _cnpj("300400500002")
    est = m.montar_estrutura("loja", _linhas_com_nome_sensivel(cnpj, outro))
    novo, regs = mascara.mascarar_estrutura(est)
    m.validar(novo)
    texto = json.dumps(novo)
    assert cnpj not in texto and outro not in texto
    funcoes = {f["nome"]: f for f in novo["funcoes"]}
    assert set(funcoes) == {"proc_a_[mascarado:cnpj:2]", "proc_a_[mascarado:cnpj:1]"} or len(funcoes) == 2
    ref = next(f for f in novo["funcoes"] if f["nome"].endswith(":1]"))
    assert "proc_a_[mascarado:cnpj:1]" in ref["codigo"]  # nome e código coerentes
    assert any(r["campo"] == "nome" and r["tipo"] == "cnpj" for r in regs)
    assert ":1]" not in json.dumps(regs) and cnpj not in json.dumps(regs)


def test_colisao_de_nomes_apos_mascarar_falha() -> None:
    est = m.montar_estrutura("loja", linhas_exemplo())
    est["funcoes"][1] = {**est["funcoes"][0]}
    with pytest.raises(mascara.ErroMascara, match="duplicado"):
        mascara.verificar_nomes_unicos(est)


def test_extracao_duas_vezes_e_schema_unico_preservam_n(projeto: Projeto) -> None:
    cnpj, outro = _cnpj("700800900001"), _cnpj("700800900002")
    def fonte():
        f = fonte_com(["alfa", "beta"])
        f.schemas["alfa"] = _linhas_com_nome_sensivel(cnpj, outro) | {}
        for l in f.schemas["alfa"]["schemas"]:
            l["schema"] = "alfa"
        for lista in f.schemas["alfa"].values():
            for l in lista:
                if "schema" in l:
                    l["schema"] = "alfa"
                if "schemaname" in l:
                    l["schemaname"] = "alfa"
        f.schemas["beta"] = copy.deepcopy(f.schemas["alfa"])
        f.schemas["beta"]["funcoes"][0]["nome"] = f"outra_{outro}"
        for lista in f.schemas["beta"].values():
            for l in lista:
                if "schema" in l:
                    l["schema"] = "beta"
        return f
    catalogo.extrair(projeto, fonte(), relogio=relogio, imprimir=lambda _: None)
    est = projeto.doc / "catalogo" / "estrutura"
    antes = {p.name: p.read_bytes() for p in est.iterdir()}
    mapa = projeto.controle / mascara.NOME_MAPA
    assert mapa.is_file() and len(json.loads(mapa.read_text(encoding="utf-8"))["cnpj"]) == 2
    catalogo.extrair(projeto, fonte(), relogio=relogio, imprimir=lambda _: None)
    assert {p.name: p.read_bytes() for p in est.iterdir()} == antes
    mapa.unlink()  # sem mapa, reextrair tudo reproduz a mesma numeração
    catalogo.extrair(projeto, fonte(), relogio=relogio, imprimir=lambda _: None)
    assert {p.name: p.read_bytes() for p in est.iterdir()} == antes
    catalogo.extrair(projeto, fonte(), schema="beta", relogio=relogio, imprimir=lambda _: None)
    assert {p.name: p.read_bytes() for p in est.iterdir()} == antes
    assert catalogo.verificar_mascara(projeto.doc / "catalogo", lambda _: None) == 0
    reg = (projeto.controle / "mascaramento.json").read_text(encoding="utf-8")
    assert cnpj not in reg and outro not in reg


def test_extracao_exige_mapa_no_gitignore(projeto: Projeto) -> None:
    (projeto.doc / ".gitignore").write_text("outra_coisa\n", encoding="utf-8")
    with pytest.raises(ErroConfig, match="gitignore"):
        catalogo.extrair(projeto, fonte_com(["alfa"]), relogio=relogio, imprimir=lambda _: None)
    assert not (projeto.controle / mascara.NOME_MAPA).exists()


# ----------------------------------------------------------------- D-010: credenciais


C = "[mascarado:credencial]"


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("host=192.0.2.10 user=fulano password=segredo",
         f"host={C} user={C} password={C}"),
        ("'host=192.0.2.10 port=5432 dbname=bd user=fulano password=segredo'",
         f"'host={C} port={C} dbname={C} user={C} password={C}'"),
        ("'host=''h.exemplo.teste'' user=''fulano'' password=''segredo'''",
         f"'host={C} user={C} password={C}'"),
        ("HOST = a USER = 'b' Password = \"c\"", f"HOST = {C} USER = {C} Password = {C}"),
        ("WHERE password = 'segredo'", f"WHERE password = {C}"),
        ("postgresql://fulano:segredo@192.0.2.10:5432/bd", f"postgresql://{C}"),
        ("CREATE USER MAPPING FOR u SERVER s OPTIONS (user 'fulano', password 'segredo')",
         f"CREATE USER MAPPING FOR u SERVER s OPTIONS (user '{C}', password '{C}')"),
        ("conecta em 192.0.2.10 e 10.0.0.1", "conecta em [mascarado:ip] e [mascarado:ip]"),
    ],
)  # fmt: skip
def test_mascara_credenciais_positivos(texto: str, esperado: str) -> None:
    novo, cont = mascara.mascarar(texto)
    assert novo == esperado and cont
    assert mascara.encontrar(novo) == {}  # idempotente e reconhecido pelo verificador
    assert mascara.mascarar(novo)[0] == novo


@pytest.mark.parametrize(
    "texto",
    [
        "'host=' || servidor || ' user=' || usuario || ' password=' || senha",
        "SELECT * FROM t WHERE user = 'x'",
        "-- host=abc",
        "user=a AND x=1",
        "versão 15.5.1",
        "999.1.1.1 e 1.2.3",
        "SELECT user=1",
    ],
)
def test_mascara_credenciais_negativos(texto: str) -> None:
    assert mascara.mascarar(texto) == (texto, {})


def test_credencial_em_rotina_registra_sem_valor() -> None:
    linhas = linhas_exemplo()
    linhas["funcoes"][0]["codigo"] = "PERFORM dblink('host=192.0.2.10 user=fulano password=segredo')"
    est, regs = mascara.mascarar_estrutura(m.montar_estrutura("loja", linhas))
    m.validar(est)
    tipos = {(r["tipo"], r["objeto"], r["quantidade"]) for r in regs}
    assert ("credencial", "funcao:somar", 3) in tipos
    assert "segredo" not in json.dumps(est) and "192.0.2.10" not in json.dumps(regs)
    assert mascara.varrer_documento(est) == []


@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("v_senha := 'abc123'", "v_senha := '[mascarado:credencial]'"),
        ("UPDATE t SET usuario_senha = 'x1'", "UPDATE t SET usuario_senha = '[mascarado:credencial]'"),
        ("pwd='x'", "pwd='[mascarado:credencial]'"),
        ("criar(p_PASSWORD => 'x')", "criar(p_PASSWORD => '[mascarado:credencial]')"),
        ("\"senha\" = 'x'", "\"senha\" = '[mascarado:credencial]'"),
        ("EXECUTE 'ALTER ROLE r WITH passwd=''x''", "EXECUTE 'ALTER ROLE r WITH passwd=''[mascarado:credencial]''"),
        ("passwd = 'x' ::text", "passwd = '[mascarado:credencial]' ::text"),
    ],
)
def test_mascara_atribuicao_de_senha_literal(texto: str, esperado: str) -> None:
    saida, contagem = mascara.mascarar(texto)
    assert saida == esperado and contagem == {"credencial": 1}
    assert mascara.mascarar(saida) == (saida, {})  # idempotente


@pytest.mark.parametrize(
    "texto",
    [
        "v_senha := p_senha",
        "senha = md5(p_senha)",
        "senha = ''",
        "senha := 'a' || v_x",
        "senha_hash := crypt(p_senha, gen_salt('bf'))",
        "senha IS NULL",
        "WHERE senha <> 'x' AND tipo = 'abc'",
        "nome = 'x'",
        "v_senha = $1",
        "senha := '[mascarado:credencial]'",
    ],
)
def test_mascara_atribuicao_de_senha_negativos(texto: str) -> None:
    assert mascara.mascarar(texto) == (texto, {})
