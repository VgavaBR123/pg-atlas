"""Trava de processo único, backoff, sanitização e fluxo da conexão (sem banco)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import errors as pgerr

from conftest import ENV_FALSO, escrever_env
from pg_atlas.config import carregar_projeto
from pg_atlas.conexao import (
    Conexao,
    TravaProcessoUnico,
    pid_vivo,
    verificar_credenciais,
)
from pg_atlas.erros import (
    ErroConexao,
    ErroCredenciais,
    ErroExecucao,
    ErroTravaOcupada,
    SqlRejeitado,
)

# ------------------------------------------------------------------ trava


def test_trava_adquire_e_libera(tmp_path: Path) -> None:
    trava = TravaProcessoUnico(tmp_path / "_controle" / "db.lock")
    trava.adquirir()
    assert json.loads(trava.caminho.read_text())["pid"] == os.getpid()
    trava.liberar()
    assert not trava.caminho.exists()


def test_trava_ocupada_por_processo_vivo(tmp_path: Path) -> None:
    caminho = tmp_path / "db.lock"
    caminho.write_text(json.dumps({"pid": 424242}))
    trava = TravaProcessoUnico(caminho, vivo=lambda pid: pid == 424242)
    with pytest.raises(ErroTravaOcupada) as erro:
        trava.adquirir()
    assert "424242" in str(erro.value)
    assert caminho.exists()  # não apagou a trava de quem está vivo


def test_trava_orfa_e_removida(tmp_path: Path) -> None:
    caminho = tmp_path / "db.lock"
    caminho.write_text(json.dumps({"pid": 424242}))
    trava = TravaProcessoUnico(caminho, vivo=lambda pid: False)
    trava.adquirir()
    assert json.loads(caminho.read_text())["pid"] == os.getpid()
    trava.liberar()


def test_trava_do_proprio_processo_nao_reentra(tmp_path: Path) -> None:
    primeira = TravaProcessoUnico(tmp_path / "db.lock", vivo=lambda pid: False)
    segunda = TravaProcessoUnico(tmp_path / "db.lock", vivo=lambda pid: False)
    primeira.adquirir()
    with pytest.raises(ErroTravaOcupada):
        segunda.adquirir()
    primeira.liberar()
    segunda.adquirir()
    segunda.liberar()


def test_trava_corrompida_recente_nao_e_apagada(tmp_path: Path) -> None:
    caminho = tmp_path / "db.lock"
    caminho.write_text("lixo")
    with pytest.raises(ErroTravaOcupada):
        TravaProcessoUnico(caminho).adquirir()
    assert caminho.exists()


def test_trava_corrompida_antiga_e_orfa(tmp_path: Path) -> None:
    caminho = tmp_path / "db.lock"
    caminho.write_text("lixo")
    antigo = caminho.stat().st_mtime - 3600
    os.utime(caminho, (antigo, antigo))
    trava = TravaProcessoUnico(caminho)
    trava.adquirir()
    trava.liberar()


def test_liberar_nao_apaga_trava_alheia(tmp_path: Path) -> None:
    caminho = tmp_path / "db.lock"
    trava = TravaProcessoUnico(caminho)
    trava.adquirir()
    caminho.write_text(json.dumps({"pid": 1}))
    trava.liberar()
    assert caminho.exists()


def test_pid_vivo_reconhece_o_proprio_processo() -> None:
    assert pid_vivo(os.getpid())
    assert not pid_vivo(0)


# ------------------------------------------------------------------ credenciais


def test_credenciais_sem_env(workspace: Path) -> None:
    resultado = verificar_credenciais(workspace)
    assert not resultado.ok and not resultado.arquivo_existe
    assert str(workspace / ".env") in resultado.mensagem()
    assert ".env.example" in resultado.mensagem()


def test_credenciais_incompletas(workspace: Path) -> None:
    escrever_env(workspace, {**ENV_FALSO, "PGPASSWORD": "", "PGHOST": "  "})
    resultado = verificar_credenciais(workspace)
    assert not resultado.ok
    assert set(resultado.faltando) == {"PGPASSWORD", "PGHOST"}
    mensagem = resultado.mensagem()
    assert "PGPASSWORD" in mensagem and ENV_FALSO["PGUSER"] not in mensagem


def test_credenciais_completas_nao_devolvem_valores(workspace: Path) -> None:
    escrever_env(workspace, ENV_FALSO)
    resultado = verificar_credenciais(workspace)
    assert resultado.ok
    for valor in ENV_FALSO.values():
        assert valor not in repr(resultado)


def test_credenciais_por_servico(workspace: Path) -> None:
    escrever_env(workspace, {"PGSERVICE": "meu_servico"})
    assert verificar_credenciais(workspace).ok


def test_credenciais_por_servico_no_ambiente(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGSERVICE", "meu_servico")
    assert verificar_credenciais(workspace).ok


# ------------------------------------------------------------------ conexão falsa


class CursorFalso:
    def __init__(self, conn: ConexaoFalsa) -> None:
        self.conn = conn
        self.description: Any = None
        self._linhas: list[tuple[Any, ...]] = []

    def __enter__(self) -> CursorFalso:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.executados.append(sql)
        if self.conn.erro_na_consulta is not None:
            raise self.conn.erro_na_consulta
        if sql.startswith("SHOW default_transaction_read_only"):
            self._linhas = [(self.conn.read_only_padrao,)]
        else:
            self._linhas = [(1,)]
        self.description = [("x",)]

    def fetchone(self) -> tuple[Any, ...]:
        return self._linhas[0]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._linhas


class ConexaoFalsa:
    def __init__(self) -> None:
        self.executados: list[str] = []
        self.read_only_padrao = "on"
        self.erro_na_consulta: Exception | None = None
        self.autocommit = True
        self.read_only: bool | None = None
        self.fechada = False
        self.rollbacks = 0

    def cursor(self) -> CursorFalso:
        return CursorFalso(self)

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.fechada = True


@pytest.fixture
def projeto(doc: Path, workspace: Path):
    escrever_env(workspace, ENV_FALSO)
    return carregar_projeto(doc)


def fabrica(*resultados: Any):
    """Cada chamada devolve/lança o próximo resultado; registra os kwargs recebidos."""
    chamadas: list[dict[str, Any]] = []
    fila = list(resultados)

    def conectar(**kwargs: Any) -> Any:
        chamadas.append(kwargs)
        item = fila.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    conectar.chamadas = chamadas  # type: ignore[attr-defined]
    return conectar


def test_sessao_somente_leitura_e_registro(projeto) -> None:
    falsa = ConexaoFalsa()
    conectar = fabrica(falsa)
    with Conexao(projeto, conectar=conectar) as conexao:
        assert falsa.read_only is True and falsa.autocommit is False
        assert conexao.executar("SELECT 1") == [(1,)]
        assert (projeto.controle / "db.lock").exists()
    assert not (projeto.controle / "db.lock").exists()
    assert falsa.fechada
    argumentos = conectar.chamadas[0]
    assert argumentos["application_name"] == "pg-atlas"
    assert argumentos["host"] == ENV_FALSO["PGHOST"]  # chega ao driver, mas só aí
    ajustes = [s for s in falsa.executados if s.startswith("SELECT set_config")]
    assert len(ajustes) == 6
    log = [json.loads(linha) for linha in (projeto.controle / "sql.log.jsonl").read_text().splitlines()]
    sqls = [r for r in log if r["tipo"] == "sql"]
    assert sqls == [{**sqls[0], "sql": "SELECT 1", "resultado": "executado"}]
    texto_log = (projeto.controle / "sql.log.jsonl").read_text()
    for valor in ENV_FALSO.values():
        assert valor not in texto_log


def test_sessao_que_nao_fica_somente_leitura_aborta(projeto) -> None:
    falsa = ConexaoFalsa()
    falsa.read_only_padrao = "off"
    with pytest.raises(ErroConexao):
        Conexao(projeto, conectar=fabrica(falsa)).abrir()
    assert falsa.fechada
    assert not (projeto.controle / "db.lock").exists()


def test_guarda_barra_antes_do_banco_e_registra(projeto) -> None:
    falsa = ConexaoFalsa()
    with Conexao(projeto, conectar=fabrica(falsa)) as conexao:
        antes = len(falsa.executados)
        for sql in ("DELETE FROM t", "SELECT most_common_vals FROM pg_stats", "SELECT 1; SELECT 2"):
            with pytest.raises(SqlRejeitado):
                conexao.executar(sql)
        assert len(falsa.executados) == antes  # nada chegou ao banco
    linhas = [json.loads(x) for x in (projeto.controle / "sql.log.jsonl").read_text().splitlines()]
    assert [r["resultado"] for r in linhas if r["tipo"] == "sql"] == ["rejeitado"] * 3


def test_executar_sem_conexao_ainda_passa_pelo_guarda(projeto) -> None:
    conexao = Conexao(projeto, conectar=fabrica())
    with pytest.raises(SqlRejeitado):
        conexao.executar("DROP TABLE t")
    with pytest.raises(ErroConexao):
        conexao.executar("SELECT 1")


def test_statement_timeout_precedencia(doc: Path, workspace: Path) -> None:
    escrever_env(workspace, ENV_FALSO)
    projeto = carregar_projeto(doc)
    conexao = Conexao(projeto, conectar=fabrica(ConexaoFalsa()))
    conexao.abrir()
    assert conexao.statement_timeout_ms == 45000  # projeto.yml
    conexao.fechar()

    escrever_env(workspace, {**ENV_FALSO, "PG_ATLAS_STATEMENT_TIMEOUT": "7000"})
    conexao = Conexao(projeto, conectar=fabrica(ConexaoFalsa()))
    conexao.abrir()
    assert conexao.statement_timeout_ms == 7000  # .env vence o projeto.yml
    conexao.fechar()

    escrever_env(workspace, {**ENV_FALSO, "PG_ATLAS_STATEMENT_TIMEOUT": "abc"})
    with pytest.raises(ErroCredenciais):
        Conexao(projeto, conectar=fabrica(ConexaoFalsa())).abrir()
    assert not (projeto.controle / "db.lock").exists()


def test_sem_env_falha_com_caminho(doc: Path, workspace: Path) -> None:
    conexao = Conexao(carregar_projeto(doc), conectar=fabrica())
    with pytest.raises(ErroCredenciais) as erro:
        conexao.abrir()
    assert str(workspace / ".env") in str(erro.value)


# ------------------------------------------------------------------ backoff


def test_backoff_30_60_120_depois_aborta(projeto) -> None:
    esperas: list[float] = []
    avisos: list[str] = []
    erros = [pgerr.TooManyConnections("sorry, too many clients already") for _ in range(4)]
    conectar = fabrica(*erros)
    conexao = Conexao(projeto, conectar=conectar, dormir=esperas.append, avisar=avisos.append)
    with pytest.raises(ErroConexao) as erro:
        conexao.abrir()
    assert esperas == [30, 60, 120]
    assert len(conectar.chamadas) == 4
    assert "abortada" in str(erro.value) and "30, 60, 120" in str(erro.value)
    assert len(avisos) == 3
    assert not (projeto.controle / "db.lock").exists()


def test_backoff_recupera_na_segunda_tentativa(projeto) -> None:
    esperas: list[float] = []
    falsa = ConexaoFalsa()
    conectar = fabrica(pgerr.TooManyConnections("x"), falsa)
    with Conexao(projeto, conectar=conectar, dormir=esperas.append, avisar=lambda _: None):
        pass
    assert esperas == [30]


def test_backoff_injetavel(projeto) -> None:
    esperas: list[float] = []
    conectar = fabrica(*[pgerr.TooManyConnections("x") for _ in range(3)])
    with pytest.raises(ErroConexao):
        Conexao(
            projeto, conectar=conectar, dormir=esperas.append, backoff_s=(0.1, 0.2), avisar=lambda _: None
        ).abrir()
    assert esperas == [0.1, 0.2]


def test_outro_erro_de_conexao_nao_espera(projeto) -> None:
    esperas: list[float] = []
    conectar = fabrica(pgerr.InvalidPassword("falha"))
    with pytest.raises(ErroConexao) as erro:
        Conexao(projeto, conectar=conectar, dormir=esperas.append).abrir()
    assert esperas == [] and "autenticação" in str(erro.value)


# ------------------------------------------------------------------ sanitização


def test_erro_de_conexao_nao_vaza_segredos(projeto) -> None:
    vazamento = (
        f'connection to server at "{ENV_FALSO["PGHOST"]}", port 5432 failed: '
        f'FATAL: password authentication failed for user "{ENV_FALSO["PGUSER"]}"'
    )
    conectar = fabrica(psycopg.OperationalError(vazamento))
    with pytest.raises(ErroConexao) as erro:
        Conexao(projeto, conectar=conectar).abrir()
    mensagem = str(erro.value)
    for valor in ENV_FALSO.values():
        assert valor not in mensagem
    assert erro.value.__cause__ is None and erro.value.__suppress_context__


def test_erro_generico_de_conexao_nao_vaza_segredos(projeto) -> None:
    vazamento = f'could not translate host name "{ENV_FALSO["PGHOST"]}" to address'
    with pytest.raises(ErroConexao) as erro:
        Conexao(projeto, conectar=fabrica(psycopg.OperationalError(vazamento))).abrir()
    for valor in ENV_FALSO.values():
        assert valor not in str(erro.value)


def test_erro_de_execucao_e_saneado(projeto) -> None:
    falsa = ConexaoFalsa()
    with Conexao(projeto, conectar=fabrica(falsa)) as conexao:
        falsa.erro_na_consulta = psycopg.errors.UndefinedTable("falha")
        with pytest.raises(ErroExecucao) as erro:
            conexao.executar("SELECT 1")
    assert "sqlstate" in str(erro.value)
    assert erro.value.__cause__ is None and erro.value.__suppress_context__
    assert falsa.rollbacks >= 1


def test_erro_de_dados_omite_detalhe(projeto) -> None:
    falsa = ConexaoFalsa()
    with Conexao(projeto, conectar=fabrica(falsa)) as conexao:
        falsa.erro_na_consulta = psycopg.errors.InvalidTextRepresentation("valor secreto")
        with pytest.raises(ErroExecucao) as erro:
            conexao.executar("SELECT 1")
    assert "secreto" not in str(erro.value)


# ------------------------------------------------------------------ exceção do guarda na sessão


def test_so_a_abertura_de_sessao_executa_sql_sem_guarda() -> None:
    """`cur.execute` só pode aparecer em `executar` (com guarda) e `_configurar_sessao`
    (apenas constantes do módulo)."""
    import ast

    import pg_atlas.conexao as modulo

    arvore = ast.parse(Path(modulo.__file__).read_text(encoding="utf-8"))
    achados: dict[str, list[ast.Call]] = {}
    for funcao in ast.walk(arvore):
        if isinstance(funcao, ast.FunctionDef):
            for no in ast.walk(funcao):
                if (
                    isinstance(no, ast.Call)
                    and isinstance(no.func, ast.Attribute)
                    and no.func.attr == "execute"
                ):
                    achados.setdefault(funcao.name, []).append(no)
    assert set(achados) <= {"_configurar_sessao", "executar", "execute"}
    for chamada in achados["_configurar_sessao"]:
        primeiro = chamada.args[0]
        assert isinstance(primeiro, ast.Name) and primeiro.id.startswith("SQL_SESSAO_")
    # `executar` chama o guarda antes de qualquer execute
    fonte = ast.get_source_segment(Path(modulo.__file__).read_text(encoding="utf-8"),
                                   next(f for f in ast.walk(arvore)
                                        if isinstance(f, ast.FunctionDef) and f.name == "executar"))
    assert fonte.index("guarda.verificar") < fonte.index(".execute(")


def test_set_config_vindo_de_fora_e_barrado(projeto) -> None:
    falsa = ConexaoFalsa()
    with Conexao(projeto, conectar=fabrica(falsa)) as conexao:
        with pytest.raises(SqlRejeitado):
            conexao.executar("SELECT set_config('default_transaction_read_only', 'off', false)")
        with pytest.raises(SqlRejeitado):
            conexao.executar("SHOW default_transaction_read_only; SELECT 1")
