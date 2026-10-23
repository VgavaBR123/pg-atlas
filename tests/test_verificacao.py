"""Testes offline do comando `verificar catalogo` (tarefa 1.5). Nomes e dados são fictícios."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from test_catalogo import _cnpj, _formatar_cnpj, fonte_com, relogio

from pg_atlas import catalogo, catalogo_sql, cli, esqueleto, guarda, triagem
from pg_atlas import verificacao as ver
from pg_atlas import verificacao_checagens as chk
from pg_atlas.config import Projeto


@pytest.fixture
def projeto(tmp_path: Path) -> Projeto:
    """Documentação fictícia completa: catálogo, triagem e esqueleto gerados de verdade."""
    doc = tmp_path / "doc"
    (doc / "_controle").mkdir(parents=True)
    (doc / "config").mkdir()
    (doc / ".gitignore").write_text("_controle/mascaramento-mapa.local.json\n", encoding="utf-8")
    estado = {"fases": {"1": {"tarefas": {"1.2": {"descricao": "Extrator"}}}}}
    (doc / "_controle" / "estado.json").write_text(json.dumps(estado), encoding="utf-8")
    p = Projeto(doc=doc, workspace=tmp_path, dados={"escopo": {"schemas": "todos", "excluir": []}})
    catalogo.extrair(p, fonte_com(["alfa", "beta"]), relogio=relogio, imprimir=lambda _: None)
    triagem.executar_triagem(doc, p.controle)
    esqueleto.executar_esqueleto(doc, p.controle, "Teste", "Banco de teste")
    return p


class FonteContagem:
    """Imita a `Conexao` para a verificação: contagens e lista de schemas fictícias."""

    def __init__(self, contagens: dict[str, dict[str, int]], escopo: list[str] | None = None) -> None:
        self.contagens = contagens
        self.escopo = list(contagens) if escopo is None else escopo
        self.consultas: list[str] = []

    def executar_com_colunas(self, sql: str, params: Any = None):
        if sql == ver.carregar_sql("contagens"):
            self.consultas.append("contagens")
            colunas = ["schema", *ver.CAMPOS_CONTAGEM]
            return colunas, [(s, *(c[k] for k in ver.CAMPOS_CONTAGEM)) for s, c in self.contagens.items()]
        assert sql == catalogo_sql.carregar("schemas") and params == {"schema": None}
        self.consultas.append("schemas")
        return ["schema", "dono", "comentario"], [(s, "d", None) for s in self.escopo]


def contagens_reais(p: Projeto) -> dict[str, dict[str, int]]:
    """Contagem do banco fictício = a do catálogo gerado (só os campos consultados)."""
    raiz = p.doc / "catalogo"
    todas = chk.contagens_do_catalogo(raiz, chk.schemas_do_banco_json(raiz))
    return {s: {k: c[k] for k in ver.CAMPOS_CONTAGEM} for s, c in todas.items()}


def por_nome(resultados: list[chk.Verificacao]) -> dict[str, chk.Verificacao]:
    return {r.nome: r for r in resultados}


# ----------------------------------------------------------------- tudo certo


def test_catalogo_integro_passa_em_todas(projeto: Projeto) -> None:
    resultados = ver.verificar_catalogo(projeto, FonteContagem(contagens_reais(projeto)))
    assert [r.nome for r in resultados] == ["contagem", "esquemas", "proibidos", "fichas", "cobertura"]
    assert all(r.ok for r in resultados), [r.problemas for r in resultados]


def test_sem_banco_pula_so_a_contagem(projeto: Projeto) -> None:
    assert [r.nome for r in ver.verificar_catalogo(projeto, None)] == [
        "esquemas", "proibidos", "fichas", "cobertura",
    ]


# ----------------------------------------------------------------- contagem


def test_contagem_divergente_falha(projeto: Projeto) -> None:
    banco = contagens_reais(projeto)
    banco["alfa"]["tabela"] += 1
    v = ver.verificar_contagem(projeto, FonteContagem(banco))
    assert not v.ok
    assert v.problemas == [f"schema 'alfa', tabela: catálogo {banco['alfa']['tabela'] - 1}, banco {banco['alfa']['tabela']}"]


def test_contagem_funcoes_e_sequencias_divergentes(projeto: Projeto) -> None:
    banco = contagens_reais(projeto)
    banco["beta"]["funcoes"] += 2
    banco["beta"]["sequencias"] += 1
    v = ver.verificar_contagem(projeto, FonteContagem(banco))
    assert len(v.problemas) == 2 and all("schema 'beta'" in p for p in v.problemas)


def test_schema_do_escopo_sem_arquivo_falha(projeto: Projeto) -> None:
    banco = contagens_reais(projeto)
    banco["gama"] = dict(banco["alfa"])
    v = ver.verificar_contagem(projeto, FonteContagem(banco))
    assert v.problemas == ["schema 'gama' existe no banco (escopo) e não tem arquivo no catálogo"]


def test_schema_fora_do_escopo_nao_exige_arquivo(projeto: Projeto) -> None:
    banco = contagens_reais(projeto)
    banco["gama"] = dict(banco["alfa"])
    assert ver.verificar_contagem(projeto, FonteContagem(banco, escopo=["alfa", "beta"])).ok


def test_schema_do_catalogo_que_sumiu_do_banco_falha(projeto: Projeto) -> None:
    banco = contagens_reais(projeto)
    del banco["beta"]
    v = ver.verificar_contagem(projeto, FonteContagem(banco))
    assert v.problemas == ["schema 'beta' está no catálogo e não existe no banco"]


def test_sql_de_contagem_passa_no_guarda_e_so_usa_catalogo() -> None:
    sql = ver.carregar_sql("contagens")
    guarda.verificar(sql)
    texto = sql.lower()
    assert "pg_stats" not in texto and "pg_statistic" not in texto
    assert texto.count("extension") == 0 and "deptype = 'e'" in texto


# ----------------------------------------------------------------- esquemas


def alterar(caminho: Path, funcao) -> None:
    dados = json.loads(caminho.read_text(encoding="utf-8"))
    funcao(dados)
    caminho.write_text(json.dumps(dados), encoding="utf-8")


def test_json_invalido_contra_o_esquema_falha(projeto: Projeto) -> None:
    alterar(projeto.doc / "catalogo" / "estrutura" / "alfa.json", lambda d: d.pop("relacoes"))
    esq, _ = chk.verificar_arquivos(projeto.doc)
    assert not esq.ok and esq.problemas[0].startswith("catalogo/estrutura/alfa.json")


def test_estatisticas_na_pasta_errada_falha(projeto: Projeto) -> None:
    raiz = projeto.doc / "catalogo"
    (raiz / "estrutura" / "alfa.json").write_text(
        (raiz / "estatisticas" / "alfa.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    esq, _ = chk.verificar_arquivos(projeto.doc)
    assert any("'formato' diferente" in p for p in esq.problemas)


def test_triagem_e_fichas_invalidas_falham(projeto: Projeto) -> None:
    raiz = projeto.doc / "catalogo"
    alterar(raiz / "_triagem.json", lambda d: d.update(formato="outro"))
    alterar(raiz / "_fichas.json", lambda d: d["fichas"][0].pop("caminho"))
    esq, _ = chk.verificar_arquivos(projeto.doc)
    assert sorted(p.split(":")[0] for p in esq.problemas) == [
        "catalogo/_fichas.json", "catalogo/_triagem.json",
    ]


def test_json_ilegivel_falha(projeto: Projeto) -> None:
    (projeto.doc / "catalogo" / "estatisticas" / "beta.json").write_text("{quebrado", encoding="utf-8")
    esq, pro = chk.verificar_arquivos(projeto.doc)
    assert esq.problemas == ["catalogo/estatisticas/beta.json: JSON ilegível"] and pro.ok


# ----------------------------------------------------------------- proibidos


def test_chave_proibida_falha(projeto: Projeto) -> None:
    alterar(projeto.doc / "catalogo" / "estatisticas" / "alfa.json", lambda d: d.update(most_common_vals=["x"]))
    _, pro = chk.verificar_arquivos(projeto.doc)
    assert not pro.ok and "most_common_vals" in pro.problemas[0]


@pytest.mark.parametrize("chave", ["histogram_bounds", "most_common_freqs", "Elem_Count_Histogram", "stavalues1"])
def test_chaves_semelhantes_falham_em_qualquer_nivel(projeto: Projeto, chave: str) -> None:
    alterar(
        projeto.doc / "catalogo" / "estrutura" / "alfa.json",
        lambda d: d["relacoes"][-1]["colunas"][0].update({chave: 1}),
    )
    _, pro = chk.verificar_arquivos(projeto.doc)
    assert not pro.ok and chave in pro.problemas[0]


def test_valor_proibido_exato_falha_mas_codigo_com_o_nome_nao(projeto: Projeto) -> None:
    arq = projeto.doc / "catalogo" / "estrutura" / "alfa.json"
    alterar(arq, lambda d: d["relacoes"][0].update(comentario="select most_common_vals from x"))
    assert chk.verificar_arquivos(projeto.doc)[1].ok
    alterar(arq, lambda d: d["relacoes"][0].update(comentario="histogram_bounds"))
    assert not chk.verificar_arquivos(projeto.doc)[1].ok


def test_cnpj_sem_mascara_falha_sem_vazar_o_valor(projeto: Projeto) -> None:
    cnpj = _formatar_cnpj(_cnpj("445556660001"))
    alterar(
        projeto.doc / "catalogo" / "estrutura" / "alfa.json",
        lambda d: d["relacoes"][0].update(comentario=f"contribuinte {cnpj}"),
    )
    _, pro = chk.verificar_arquivos(projeto.doc)
    assert not pro.ok
    assert cnpj not in "".join(pro.problemas) and "cnpj" in pro.problemas[0]


# ----------------------------------------------------------------- fichas


def test_ficha_faltando_falha(projeto: Projeto) -> None:
    alvo = next((projeto.doc / "schemas" / "alfa" / "tabelas").glob("*.md"))
    alvo.unlink()
    v = chk.verificar_fichas(projeto.doc)
    assert v.problemas == [f"ficha faltando: schemas/alfa/tabelas/{alvo.name}"]


def test_ficha_sobrando_falha(projeto: Projeto) -> None:
    (projeto.doc / "schemas" / "beta" / "funcoes" / "inexistente.md").write_text("x", encoding="utf-8")
    v = chk.verificar_fichas(projeto.doc)
    assert v.problemas == ["ficha sem objeto no catálogo (órfã): schemas/beta/funcoes/inexistente.md"]


def test_mapa_sem_um_objeto_e_com_um_a_mais_falham(projeto: Projeto) -> None:
    mapa = projeto.doc / "catalogo" / "_fichas.json"
    alterar(mapa, lambda d: d["fichas"].pop())
    assert any(p.startswith("objeto sem ficha no mapa") for p in chk.verificar_fichas(projeto.doc).problemas)
    esqueleto.executar_esqueleto(projeto.doc, projeto.controle, "Teste", "Banco de teste")
    assert chk.verificar_fichas(projeto.doc).ok
    alterar(mapa, lambda d: d["fichas"].append({**d["fichas"][0], "caminho": "schemas/x/tabelas/y.md"}))
    assert any(p.startswith("ficha no mapa sem objeto") for p in chk.verificar_fichas(projeto.doc).problemas)


def test_orfas_registradas_e_catalogo_sem_triagem_falham(projeto: Projeto) -> None:
    orfas = projeto.controle / "esqueleto-orfas.json"
    alterar(orfas, lambda d: d.update(orfas=["schemas/alfa/tabelas/velha.md"]))
    assert not chk.verificar_fichas(projeto.doc).ok
    (projeto.doc / "catalogo" / "_triagem.json").unlink()
    v = chk.verificar_fichas(projeto.doc)
    assert v.problemas and "não foi possível calcular" in v.problemas[0]


def test_particoes_tem_ficha_e_gatilhos_nao(projeto: Projeto) -> None:
    v = chk.verificar_fichas(projeto.doc)
    assert v.ok and v.resumo["esperadas"] == v.resumo["no_mapa"] == v.resumo["arquivos_existentes"]
    assert (projeto.doc / "schemas" / "alfa" / "tabelas" / "cliente_p1.md").is_file()


# ----------------------------------------------------------------- cobertura


def test_schema_sem_estatisticas_falha(projeto: Projeto) -> None:
    (projeto.doc / "catalogo" / "estatisticas" / "beta.json").unlink()
    v = chk.verificar_cobertura(projeto.doc)
    assert v.problemas == ["schema 'beta' sem arquivo estatisticas/beta.json"]


def test_estado_da_1_2_incompleto_falha(projeto: Projeto) -> None:
    alterar(
        projeto.controle / "estado.json",
        lambda d: d["fases"]["1"]["tarefas"]["1.2"]["progresso"].update(concluidos=1),
    )
    v = chk.verificar_cobertura(projeto.doc)
    assert any("mostra 1/2" in p for p in v.problemas)


# ----------------------------------------------------------------- relatório e CLI


def test_comando_registrado_na_cli() -> None:
    assert "pg_atlas.verificacao" in cli.MODULOS_DE_COMANDOS
    args = cli.criar_parser().parse_args(["verificar", "catalogo", "--doc", "x", "--sem-banco"])
    assert args.sem_banco is True and args.func is ver.executar


def test_executar_grava_relatorio_deterministico_e_sai_com_zero(
    projeto: Projeto, capsys: pytest.CaptureFixture[str]
) -> None:
    fonte = FonteContagem(contagens_reais(projeto))
    assert ver.executar_catalogo(projeto, fonte) == 0
    saida = capsys.readouterr().out
    assert "[OK] contagem" in saida and "Resumo: 5 verificações OK." in saida
    arquivo = projeto.controle / ver.SAIDA
    primeiro = json.loads(arquivo.read_text(encoding="utf-8"))
    assert primeiro["ok"] is True and primeiro["banco_consultado"] is True
    assert arquivo.read_text(encoding="utf-8").endswith("}\n")
    ver.executar_catalogo(projeto, fonte)
    segundo = json.loads(arquivo.read_text(encoding="utf-8"))
    primeiro.pop("verificado_em"), segundo.pop("verificado_em")
    assert primeiro == segundo


def test_falha_devolve_codigo_diferente_de_zero(
    projeto: Projeto, capsys: pytest.CaptureFixture[str]
) -> None:
    (projeto.doc / "schemas" / "beta" / "funcoes" / "sobra.md").write_text("x", encoding="utf-8")
    assert ver.executar_catalogo(projeto, None) == 1
    saida = capsys.readouterr().out
    assert "[FALHA] fichas" in saida and "1 de 4 verificação(ões) falharam (fichas)" in saida
    relatorio = json.loads((projeto.controle / ver.SAIDA).read_text(encoding="utf-8"))
    assert relatorio["ok"] is False and relatorio["banco_consultado"] is False


def test_cli_sem_banco_de_ponta_a_ponta(projeto: Projeto) -> None:
    (projeto.doc / "config" / "projeto.yml").write_text("projeto:\n  nome: Teste\n", encoding="utf-8")
    assert cli.main(["verificar", "catalogo", "--doc", str(projeto.doc), "--sem-banco"]) == 0
    assert (projeto.controle / ver.SAIDA).is_file()


# ----------------------------------------------------------------- banco real (opcional)


@pytest.mark.banco
def test_verificar_catalogo_no_banco_real() -> None:
    """Roda a contagem real (exige `PGATLAS_DOC`); comando: `-m banco -k verificar_catalogo`."""
    doc = os.environ.get("PGATLAS_DOC")
    if not doc:
        pytest.skip("defina PGATLAS_DOC com o caminho do repositório de documentação")
    from pg_atlas.conexao import Conexao
    from pg_atlas.config import carregar_projeto

    real = carregar_projeto(doc)
    with Conexao(real) as conexao:
        v = ver.verificar_contagem(real, conexao)
    assert v.ok, v.problemas
