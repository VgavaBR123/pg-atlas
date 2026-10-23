"""Testes offline da triagem (tarefa 1.3, D-012). Catálogos sintéticos, nomes inventados."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pg_atlas import cli, triagem
from pg_atlas.erros import ErroAtlas

BASE = "2026-01-01T00:00:00+00:00"


def rel(nome: str, tipo: str = "tabela", acesso: str = "ok", pai: tuple[str, str] | None = None) -> dict:
    particao = {"pai_schema": pai[0], "pai_nome": pai[1], "limite": "x"} if pai else None
    return {"nome": nome, "tipo": tipo, "acesso": acesso, "particao": particao}


def est(nome: str, estimadas: Any = 10, vivas: Any = 10, tipo: str = "tabela") -> dict:
    return {"nome": nome, "tipo": tipo, "linhas_estimadas": estimadas, "linhas_vivas": vivas}


def montar_doc(tmp_path: Path, schemas: dict[str, tuple[list[dict], list[dict]]]) -> Path:
    """Cria <doc>/catalogo, config/projeto.yml e _controle com o catálogo sintético."""
    doc = tmp_path / "doc"
    (doc / "config").mkdir(parents=True)
    (doc / "config" / "projeto.yml").write_text('projeto:\n  nome: "t"\n', encoding="utf-8")
    (doc / "_controle").mkdir()
    cat = doc / "catalogo"
    for pasta in ("estrutura", "estatisticas"):
        (cat / pasta).mkdir(parents=True)
    for schema, (relacoes, estatisticas) in schemas.items():
        (cat / "estrutura" / f"{schema}.json").write_text(
            json.dumps({"formato": "estrutura", "schema": schema, "relacoes": relacoes}),
            encoding="utf-8",
        )
        (cat / "estatisticas" / f"{schema}.json").write_text(
            json.dumps({"formato": "estatisticas", "schema": schema, "relacoes": estatisticas}),
            encoding="utf-8",
        )
    banco = {"formato": "banco", "extraido_em": BASE, "schemas": {s: {} for s in schemas}}
    (cat / "_banco.json").write_text(json.dumps(banco), encoding="utf-8")
    return doc


def triar_um(nome: str, estimadas: Any = 10, vivas: Any = 10, **kw: Any) -> dict:
    """Triagem de uma única tabela no schema `s`; devolve o item."""
    estruturas = {"s": {"relacoes": [rel(nome, **kw)]}}
    estatisticas = {"s": {"relacoes": [est(nome, estimadas, vivas)]}}
    return triagem.triar(BASE, estruturas, estatisticas)["tabelas"][0]


# ----------------------------------------------------------------- tokens e termos


def test_tokens_sem_acento_minusculas_e_separadores() -> None:
    assert triagem.tokens_do_nome("Situação-Tipo__x.y") == ["situacao", "tipo", "x", "y"]


def test_marcador_mascarado_e_um_token_unico() -> None:
    assert triagem.tokens_do_nome("proc_[mascarado:cnpj:12]_tipo") == ["proc", "[mascarado]", "tipo"]


@pytest.mark.parametrize(
    "nome,esperado",
    [
        ("tipo_pessoa", ["tipo"]),
        ("lista_de_situacoes", ["situacao"]),
        ("status", ["status"]),
        ("configuracoes_x_motivos", ["configuracao", "motivo"]),
        ("prototipo", []),  # sem substring
        ("tipoimovel", []),
        ("tp_pessoa", []),  # sem abreviação
    ],
)
def test_termos_de_dominio(nome: str, esperado: list[str]) -> None:
    assert triagem.termos_de_dominio(nome) == esperado


@pytest.mark.parametrize(
    "nome,sinal",
    [
        ("_algo", "prefixo_underscore"),
        ("pessoa_temp", "termo_temporario"),
        ("tmp_pessoa", "termo_temporario"),
        ("pessoa_bkp", "termo_temporario"),
        ("pessoa_backup", "termo_temporario"),
        ("pessoa_old", "termo_temporario"),
        ("pessoa_copia", "termo_temporario"),
        ("x_tmp_20230101", "termo_temporario"),  # ignora a data final
        ("x_old_2", "termo_temporario"),  # ignora o sufixo numérico final
    ],
)
def test_sinais_de_temporaria(nome: str, sinal: str) -> None:
    assert sinal in triagem.sinais_de_nome(nome)


@pytest.mark.parametrize("nome", ["template", "tmpx", "x_old_y", "bolder", "x_temp_y"])
def test_temporaria_so_no_primeiro_ou_ultimo_token(nome: str) -> None:
    assert "termo_temporario" not in triagem.sinais_de_nome(nome)


@pytest.mark.parametrize(
    "nome",
    [
        "x_2023", "x_1900", "x_2099", "x_202301", "x_012023", "x_20230131", "x_31012023",
        "x_202301311230", "x_20230131123059", "x_2023_01", "x_2023_01_31", "x_31_01_2023",
    ],
)
def test_sufixo_de_data_valido(nome: str) -> None:
    sinais = triagem.sinais_de_nome(nome)
    assert "sufixo_data" in sinais and "sufixo_numerico" not in sinais


@pytest.mark.parametrize(
    "nome",
    ["x_1899", "x_2100", "x_20231301", "x_20230230", "x_2023_13", "x_2023_02_30",
     "x_202313", "x_20230131256000", "x_2023x", "x2023", "x-2023", "x_12345"],
)
def test_sufixo_de_data_invalido(nome: str) -> None:
    assert "sufixo_data" not in triagem.sinais_de_nome(nome)


def test_sufixo_mascarado_conta_como_copia() -> None:
    sinais = triagem.sinais_de_nome("proc_a_[mascarado:cnpj:3]")
    assert "sufixo_mascarado" in sinais
    assert triar_um("proc_a_[mascarado:cnpj:3]")["classe"] == "temporaria_ou_copia"


@pytest.mark.parametrize("nome", ["nivel_1", "grupo_01", "x_123"])
def test_sufixo_numerico_so_informa_e_nao_muda_a_classe(nome: str) -> None:
    item = triar_um(nome)
    assert "sufixo_numerico" in item["sinais"]
    assert item["classe"] == "comum"


# ----------------------------------------------------------------- classes


def test_vazia_exige_as_duas_contagens_zero() -> None:
    assert triar_um("alfa", 0, 0)["classe"] == "vazia"
    assert triar_um("alfa", 5, 0)["classe"] != "vazia"
    assert triar_um("alfa", 0, None)["classe"] != "vazia"
    assert triar_um("alfa", -1, 0)["classe"] != "vazia"


def test_dominio_candidata_com_limite_de_500() -> None:
    assert triar_um("tipo_pessoa", 500, 3)["classe"] == "dominio_candidata"
    assert triar_um("tipo_pessoa", 501, 3)["classe"] == "comum"
    item = triar_um("tipo_pessoa", 3, 3)
    assert item["termos_dominio"] == ["tipo"] and "termo_dominio" in item["sinais"]


def test_fato_grande_acima_de_um_milhao() -> None:
    assert triar_um("alfa", 1_000_001, 1)["classe"] == "fato_grande"
    assert triar_um("alfa", 1_000_000, 1)["classe"] == "comum"


def test_comum() -> None:
    assert triar_um("alfa", 10, 10)["classe"] == "comum"


def test_precedencia_vazia_sobre_temporaria_e_dominio() -> None:
    assert triar_um("tipo_x_tmp", 0, 0)["classe"] == "vazia"


def test_precedencia_temporaria_sobre_dominio_e_registra_os_sinais() -> None:
    item = triar_um("tipo_pessoa_20230101", 5, 5)
    assert item["classe"] == "temporaria_ou_copia"
    assert {"termo_dominio", "sufixo_data", "porte_dominio"} <= set(item["sinais"])
    assert item["sinais"] == sorted(item["sinais"])


def test_precedencia_dominio_sobre_fato_nao_ocorre_mas_temporaria_vence_fato() -> None:
    assert triar_um("alfa_bkp", 2_000_000, 2_000_000)["classe"] == "temporaria_ou_copia"


# ----------------------------------------------------------------- estatística


def test_maior_valor_disponivel_define_linhas_triagem() -> None:
    item = triar_um("tipo_x", 3, 2_000_000)
    assert item["linhas_triagem"] == 2_000_000
    assert item["classe"] == "fato_grande"  # reltuples desatualizado não vira candidata


@pytest.mark.parametrize("estimadas,vivas", [(None, None), (-1, None), (-1, -1)])
def test_sem_estatistica_nao_vira_vazia_candidata_nem_fato(estimadas: Any, vivas: Any) -> None:
    item = triar_um("tipo_pessoa", estimadas, vivas)
    assert item["linhas_triagem"] is None
    assert item["classe"] == "comum"
    assert "sem_estatistica" in item["sinais"]
    assert item["linhas_estimadas"] is None and item["linhas_vivas"] is None


def test_sem_estatistica_com_nome_temporario_fica_temporaria() -> None:
    assert triar_um("alfa_old", None, None)["classe"] == "temporaria_ou_copia"


def test_relacao_sem_linha_de_estatistica() -> None:
    estruturas = {"s": {"relacoes": [rel("tipo_x")]}}
    item = triagem.triar(BASE, estruturas, {"s": {"relacoes": []}})["tabelas"][0]
    assert item["linhas_triagem"] is None and "sem_estatistica" in item["sinais"]


def test_estimada_conhecida_e_viva_desconhecida() -> None:
    item = triar_um("tipo_x", 7, None)
    assert item["linhas_triagem"] == 7
    assert item["classe"] == "dominio_candidata" and "sem_estatistica" in item["sinais"]


# ----------------------------------------------------------------- escopo, partições


def test_so_tabela_e_particionada_entram() -> None:
    estruturas = {
        "s": {
            "relacoes": [
                rel("a"), rel("b", "particionada"), rel("v", "view"),
                rel("m", "materializada"), rel("e", "estrangeira"),
            ]
        }
    }
    doc = triagem.triar(BASE, estruturas, {"s": {"relacoes": []}})
    assert [t["nome"] for t in doc["tabelas"]] == ["a", "b"]
    assert doc["resumo"]["total"] == 2


def test_particionada_soma_as_particoes_em_todos_os_niveis() -> None:
    estruturas = {
        "s": {
            "relacoes": [
                rel("tipo_fatos", "particionada"),
                rel("tipo_fatos_a", "particionada", pai=("s", "tipo_fatos")),
                rel("tipo_fatos_a1", pai=("s", "tipo_fatos_a")),
                rel("tipo_fatos_a2", pai=("s", "tipo_fatos_a")),
                rel("tipo_fatos_b", pai=("s", "tipo_fatos")),
            ]
        }
    }
    estatisticas = {
        "s": {
            "relacoes": [
                est("tipo_fatos", -1, 0, "particionada"),
                est("tipo_fatos_a", -1, 0, "particionada"),
                est("tipo_fatos_a1", 100, 90),
                est("tipo_fatos_a2", 200, 210),
                est("tipo_fatos_b", 300, None),
            ]
        }
    }
    por_nome = {t["nome"]: t for t in triagem.triar(BASE, estruturas, estatisticas)["tabelas"]}
    pai, sub = por_nome["tipo_fatos"], por_nome["tipo_fatos_a"]
    assert (pai["linhas_estimadas"], pai["linhas_vivas"]) == (600, 300)
    assert (sub["linhas_estimadas"], sub["linhas_vivas"]) == (300, 300)
    assert "soma_particoes" in pai["sinais"] and "soma_particoes" in sub["sinais"]
    assert pai["classe"] == "comum"  # 600 linhas: acima do limite de domínio
    assert pai["particao_de"] is None and sub["particao_de"] == "s.tipo_fatos"


def test_particionada_sem_estatistica_nas_particoes_fica_ausente() -> None:
    estruturas = {"s": {"relacoes": [rel("alfa", "particionada"), rel("alfa_1", pai=("s", "alfa"))]}}
    estatisticas = {"s": {"relacoes": [est("alfa", 5, 5, "particionada"), est("alfa_1", -1, None)]}}
    pai = triagem.triar(BASE, estruturas, estatisticas)["tabelas"][0]
    assert pai["linhas_triagem"] is None and "sem_estatistica" in pai["sinais"]


def test_particao_tem_precedencia_e_nao_vira_copia_nem_candidata() -> None:
    estruturas = {
        "s": {"relacoes": [rel("tipo_x", "particionada"), rel("tipo_x_20230101", pai=("s", "tipo_x"))]}
    }
    estatisticas = {"s": {"relacoes": [est("tipo_x_20230101", 0, 0)]}}
    por_nome = {t["nome"]: t for t in triagem.triar(BASE, estruturas, estatisticas)["tabelas"]}
    part = por_nome["tipo_x_20230101"]
    assert part["classe"] == "particao" and part["particao_de"] == "s.tipo_x"
    assert "sufixo_data" in part["sinais"] and "particao" in part["sinais"]
    assert por_nome["tipo_x"]["classe"] != "particao"


# ----------------------------------------------------------------- arquivos gerados


def catalogo_misto(tmp_path: Path) -> Path:
    return montar_doc(
        tmp_path,
        {
            "zeta": ([rel("situacao_x", acesso="sem_select"), rel("fato")], [est("situacao_x", 4, 4), est("fato", 5_000_000, 5_000_000)]),
            "alfa": (
                [rel("tipo_b"), rel("tipo_a"), rel("vazia_t"), rel("lixo_bkp"), rel("tipo_ab_[mascarado:cpf:1]")],
                [est("tipo_b", 20, 20), est("tipo_a", 1, 1), est("vazia_t", 0, 0), est("lixo_bkp", 1, 1), est("tipo_ab_[mascarado:cpf:1]", 1, 1)],
            ),
        },
    )


def rodar(doc: Path) -> dict[str, Any]:
    return triagem.executar_triagem(doc, doc / "_controle")


def test_saida_validada_e_resumos(tmp_path: Path) -> None:
    doc = catalogo_misto(tmp_path)
    saida = rodar(doc)
    gravado = json.loads((doc / "catalogo" / "_triagem.json").read_text(encoding="utf-8"))
    assert gravado == saida
    assert gravado["estatisticas_extraidas_em"] == BASE
    assert [(t["schema"], t["nome"]) for t in gravado["tabelas"]] == sorted(
        (t["schema"], t["nome"]) for t in gravado["tabelas"]
    )
    assert gravado["resumo"]["por_classe"] == {
        "comum": 0, "dominio_candidata": 3, "fato_grande": 1, "particao": 0,
        "temporaria_ou_copia": 2, "vazia": 1,
    }
    assert gravado["resumo"]["por_schema"]["zeta"]["fato_grande"] == 1


def test_candidatas_ordenadas_com_sem_select_e_mascarado(tmp_path: Path) -> None:
    doc = catalogo_misto(tmp_path)
    rodar(doc)
    texto = (doc / "config" / "dominios-candidatos.yml").read_bytes().decode("utf-8")
    assert b"\r" not in texto.encode()
    import yaml

    dados = yaml.safe_load(texto)
    tabelas = [c["tabela"] for c in dados["candidatas"]]
    assert tabelas == sorted(tabelas)
    por = {c["tabela"]: c for c in dados["candidatas"]}
    assert por["zeta.situacao_x"] == {
        "tabela": "zeta.situacao_x", "linhas_estimadas": 4, "termos": ["situacao"], "acesso": "sem_select",
    }
    assert por["alfa.tipo_a"]["acesso"] == "ok"
    # nome terminado em marcador mascarado é cópia, nunca candidata
    assert "alfa.tipo_ab_[mascarado:cpf:1]" not in por
    assert "Gerado por" in texto and "sem_select" in texto


def test_sem_candidatas_gera_lista_vazia(tmp_path: Path) -> None:
    import yaml

    doc = montar_doc(tmp_path, {"s": ([rel("alfa")], [est("alfa")])})
    rodar(doc)
    assert yaml.safe_load((doc / "config" / "dominios-candidatos.yml").read_text(encoding="utf-8")) == {
        "candidatas": []
    }


def test_nao_toca_em_dominios_yml(tmp_path: Path) -> None:
    doc = catalogo_misto(tmp_path)
    alvo = doc / "config" / "dominios.yml"
    alvo.write_text("aprovadas:\n  - alfa.tipo_a\n", encoding="utf-8")
    antes = alvo.read_bytes()
    rodar(doc)
    assert alvo.read_bytes() == antes


def test_sobrescreve_dominios_candidatos(tmp_path: Path) -> None:
    doc = catalogo_misto(tmp_path)
    (doc / "config" / "dominios-candidatos.yml").write_text("lixo antigo\n", encoding="utf-8")
    rodar(doc)
    assert "lixo antigo" not in (doc / "config" / "dominios-candidatos.yml").read_text(encoding="utf-8")


def test_duas_execucoes_geram_bytes_iguais(tmp_path: Path) -> None:
    doc = catalogo_misto(tmp_path)
    arquivos = [doc / "catalogo" / "_triagem.json", doc / "config" / "dominios-candidatos.yml"]
    rodar(doc)
    primeira = [a.read_bytes() for a in arquivos]
    rodar(doc)
    assert [a.read_bytes() for a in arquivos] == primeira


def test_ordem_de_entrada_nao_altera_a_saida(tmp_path: Path) -> None:
    a = montar_doc(tmp_path / "a", {"s": ([rel("x"), rel("y")], [est("x"), est("y")])})
    b = montar_doc(tmp_path / "b", {"s": ([rel("y"), rel("x")], [est("y"), est("x")])})
    rodar(a), rodar(b)
    assert (a / "catalogo" / "_triagem.json").read_bytes() == (b / "catalogo" / "_triagem.json").read_bytes()


def test_estado_json_preserva_o_resto(tmp_path: Path) -> None:
    doc = catalogo_misto(tmp_path)
    estado = {
        "fase_atual": 1,
        "fases": {"1": {"tarefas": {"1.2": {"status": "concluida", "schemas": {"a": "concluido"}},
                                      "1.3": {"descricao": "Triagem", "status": "pendente"}}}},
        "outro": [1, 2],
    }
    (doc / "_controle" / "estado.json").write_text(json.dumps(estado), encoding="utf-8")
    saida = rodar(doc)
    novo = json.loads((doc / "_controle" / "estado.json").read_text(encoding="utf-8"))
    tarefa = novo["fases"]["1"]["tarefas"]["1.3"]
    assert tarefa["status"] == "concluida" and tarefa["descricao"] == "Triagem"
    assert tarefa["contagem_por_classe"] == saida["resumo"]["por_classe"]
    assert novo["fases"]["1"]["tarefas"]["1.2"] == estado["fases"]["1"]["tarefas"]["1.2"]
    assert novo["outro"] == [1, 2] and novo["fase_atual"] == 1


def test_estado_json_criado_se_nao_existir(tmp_path: Path) -> None:
    doc = catalogo_misto(tmp_path)
    rodar(doc)
    novo = json.loads((doc / "_controle" / "estado.json").read_text(encoding="utf-8"))
    assert novo["fases"]["1"]["tarefas"]["1.3"]["status"] == "concluida"


# ----------------------------------------------------------------- falhas e validação


def test_falha_sem_banco_json(tmp_path: Path) -> None:
    doc = catalogo_misto(tmp_path)
    (doc / "catalogo" / "_banco.json").unlink()
    with pytest.raises(ErroAtlas, match="pg_atlas catalogo"):
        rodar(doc)


def test_falha_com_catalogo_incompleto(tmp_path: Path) -> None:
    doc = catalogo_misto(tmp_path)
    (doc / "catalogo" / "estatisticas" / "zeta.json").unlink()
    with pytest.raises(ErroAtlas, match="incompleto"):
        rodar(doc)
    assert not (doc / "catalogo" / "_triagem.json").exists()


def test_validacao_rejeita_documento_invalido() -> None:
    doc = triagem.triar(BASE, {"s": {"relacoes": [rel("x")]}}, {"s": {"relacoes": []}})
    triagem.validar(doc)
    doc["tabelas"][0]["classe"] = "inventada"
    with pytest.raises(ErroAtlas, match="tabelas/0/classe"):
        triagem.validar(doc)


def test_comando_registrado_e_executa(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    doc = catalogo_misto(tmp_path)
    assert cli.main(["triagem", "--doc", str(doc)]) == 0
    assert "Triagem: 7 tabelas" in capsys.readouterr().out
    assert cli.main(["triagem", "--doc", str(tmp_path / "nao-existe")]) == 2
