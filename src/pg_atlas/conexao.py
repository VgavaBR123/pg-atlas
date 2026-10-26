"""Único ponto de conexão ao banco.

Garantias:
- sessão somente leitura (`default_transaction_read_only = on` e transação READ ONLY),
  `statement_timeout` configurável, `lock_timeout` de 5 s e `application_name = pg-atlas`;
- um processo por vez (trava `<doc>/_controle/db.lock`, com detecção de trava órfã por PID);
- espera com backoff quando o servidor está sem conexões livres;
- todo SQL passa pelo guarda e é registrado em `<doc>/_controle/sql.log.jsonl`;
- host, usuário, banco e senha nunca são impressos, gravados em log nem repetidos em
  mensagens de exceção (as exceções do psycopg são traduzidas e descartadas).
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from dotenv import dotenv_values

from pg_atlas import guarda
from pg_atlas.config import STATEMENT_TIMEOUT_PADRAO_MS, Projeto
from pg_atlas.erros import (
    ErroConexao,
    ErroCredenciais,
    ErroExecucao,
    ErroTravaOcupada,
    SqlRejeitado,
)
from pg_atlas.registro import NOME_ARQUIVO, RegistroSql

VARIAVEIS_OBRIGATORIAS = ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")
VARIAVEL_SERVICO = "PGSERVICE"
VARIAVEL_TIMEOUT = "PG_ATLAS_STATEMENT_TIMEOUT"
BACKOFF_PADRAO_S = (30, 60, 120)
APPLICATION_NAME = "pg-atlas"
LIMITE_CONEXOES_SQLSTATE = "53300"

# SQL interno da abertura de sessão. EXCEÇÃO CONSCIENTE AO GUARDA: `set_config` e `SHOW`
# não passam por `guarda.verificar` (o guarda veta `set_config`, de propósito). São textos
# constantes deste módulo, executados só por `_configurar_sessao`; nenhum SQL vindo de fora
# usa esse caminho (`executar` sempre chama o guarda). Há teste que garante isso.
SQL_SESSAO_SET_CONFIG = "SELECT set_config(%s, %s, false)"
SQL_SESSAO_CONFERIR_SOMENTE_LEITURA = "SHOW default_transaction_read_only"


# ---------------------------------------------------------------- credenciais


@dataclass(frozen=True)
class ResultadoCredenciais:
    """Resultado do teste de credenciais. Nunca carrega os valores, só os nomes que faltam."""

    ok: bool
    arquivo: Path
    arquivo_existe: bool
    faltando: tuple[str, ...]
    usa_servico: bool

    def mensagem(self) -> str:
        """Mensagem clara apontando o que preencher (sem valores)."""
        if self.ok:
            return "Credenciais presentes."
        exemplo = self.arquivo.with_name(".env.example")
        if not self.arquivo_existe:
            return (
                f"Falta {self.arquivo} (arquivo não encontrado). Copie {exemplo} para "
                f"{self.arquivo} e preencha {', '.join(VARIAVEIS_OBRIGATORIAS)} "
                f"(ou defina {VARIAVEL_SERVICO}). O arquivo nunca é versionado."
            )
        return (
            f"Preencha em {self.arquivo} as variáveis vazias ou ausentes: "
            f"{', '.join(self.faltando)} (ou defina {VARIAVEL_SERVICO})."
        )


def _ler_env(workspace: Path) -> dict[str, str]:
    """Lê `<workspace>/.env` sem tocar em `os.environ`. Valores nulos viram vazio."""
    arquivo = workspace / ".env"
    if not arquivo.is_file():
        return {}
    return {k: (v or "").strip() for k, v in dotenv_values(arquivo, encoding="utf-8").items()}


def verificar_credenciais(workspace: Path | str) -> ResultadoCredenciais:
    """Só testa se as cinco variáveis (ou PGSERVICE) estão preenchidas; não devolve valores."""
    pasta = Path(workspace)
    arquivo = pasta / ".env"
    existe = arquivo.is_file()
    valores = _ler_env(pasta)
    servico = valores.get(VARIAVEL_SERVICO) or os.environ.get(VARIAVEL_SERVICO, "").strip()
    faltando = tuple(v for v in VARIAVEIS_OBRIGATORIAS if not valores.get(v))
    ok = bool(servico) or not faltando
    return ResultadoCredenciais(
        ok=ok,
        arquivo=arquivo,
        arquivo_existe=existe,
        faltando=() if ok else faltando,
        usa_servico=bool(servico),
    )


# ---------------------------------------------------------------- trava


def pid_vivo(pid: int) -> bool:
    """Diz se há um processo com esse PID. (Em Windows `os.kill(pid, 0)` mataria o processo.)"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        processo = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not processo:
            return ctypes.get_last_error() == 5  # acesso negado: existe, mas é de outro usuário
        try:
            codigo = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(processo, ctypes.byref(codigo)):
                return True
            return codigo.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(processo)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class TravaProcessoUnico:
    """Arquivo de trava com o PID dono. Trava órfã (PID morto) é removida."""

    IDADE_MINIMA_CORROMPIDA_S = 60

    def __init__(self, caminho: Path, *, vivo: Callable[[int], bool] = pid_vivo) -> None:
        self.caminho = Path(caminho)
        self._vivo = vivo
        self._minha = False

    def _pid_gravado(self) -> int | None:
        try:
            return int(json.loads(self.caminho.read_text(encoding="utf-8"))["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def adquirir(self) -> None:
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        for _tentativa in range(2):
            try:
                fd = os.open(self.caminho, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                self._tratar_existente()
                continue
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as arquivo:
                arquivo.write(json.dumps({"pid": os.getpid()}) + "\n")
            self._minha = True
            return
        raise ErroTravaOcupada(f"Não foi possível obter a trava {self.caminho}.")

    def _tratar_existente(self) -> None:
        pid = self._pid_gravado()
        if pid is None:
            try:
                idade = time.time() - self.caminho.stat().st_mtime
            except FileNotFoundError:
                return
            if idade < self.IDADE_MINIMA_CORROMPIDA_S:
                raise ErroTravaOcupada(
                    f"A trava {self.caminho} existe e não pôde ser lida; tente de novo em instantes."
                )
        elif pid == os.getpid() or self._vivo(pid):
            raise ErroTravaOcupada(
                f"Outro processo (PID {pid}) está conectado ao banco (trava {self.caminho}). "
                "Só um processo por vez."
            )
        try:
            self.caminho.unlink()  # trava órfã
        except FileNotFoundError:
            pass

    def liberar(self) -> None:
        if self._minha and self._pid_gravado() == os.getpid():
            try:
                self.caminho.unlink()
            except FileNotFoundError:
                pass
        self._minha = False


# ---------------------------------------------------------------- conexão


def _sanear(texto: str, segredos: Sequence[str]) -> str:
    for segredo in segredos:
        if segredo:
            texto = texto.replace(segredo, "***")
    return texto


def _classificar_falha_de_conexao(erro: psycopg.Error) -> tuple[str, bool]:
    """Devolve (mensagem fixa e segura, é_limite_de_conexoes)."""
    sqlstate = getattr(erro, "sqlstate", None) or ""
    texto = str(erro).lower()
    if sqlstate == LIMITE_CONEXOES_SQLSTATE or "too many connections" in texto or (
        "remaining connection slots" in texto
    ):
        return "o servidor está sem conexões livres", True
    if sqlstate.startswith("28") or "authentication failed" in texto:
        return "autenticação recusada (confira as credenciais do .env)", False
    if sqlstate == "3D000" or "does not exist" in texto:
        return "banco, role ou serviço inexistente (confira o .env)", False
    if "timeout" in texto or "timed out" in texto:
        return "tempo esgotado ao conectar", False
    detalhe = f"sqlstate {sqlstate}" if sqlstate else "servidor inalcançável ou configuração inválida"
    return f"falha ao conectar ({detalhe})", False


class Conexao:
    """Conexão única, somente leitura, com guarda e registro. Use como gerenciador de contexto."""

    def __init__(
        self,
        projeto: Projeto,
        *,
        conectar: Callable[..., Any] | None = None,
        dormir: Callable[[float], None] = time.sleep,
        backoff_s: Sequence[float] = BACKOFF_PADRAO_S,
        vivo: Callable[[int], bool] = pid_vivo,
        avisar: Callable[[str], None] | None = None,
        statement_timeout_ms: int | None = None,
    ) -> None:
        self.projeto = projeto
        self._conectar = conectar or psycopg.connect
        self._dormir = dormir
        self._backoff_s = tuple(backoff_s)
        self._avisar = avisar or (lambda msg: print(msg, file=sys.stderr))
        self._statement_timeout_override = statement_timeout_ms
        controle = projeto.controle
        self.registro = RegistroSql(controle / NOME_ARQUIVO)
        self.trava = TravaProcessoUnico(controle / "db.lock", vivo=vivo)
        self._conn: Any = None
        self._segredos: list[str] = []
        self.statement_timeout_ms = STATEMENT_TIMEOUT_PADRAO_MS

    # -- ciclo de vida

    def __enter__(self) -> Conexao:
        self.abrir()
        return self

    def __exit__(self, *_: object) -> None:
        self.fechar()

    def abrir(self) -> None:
        credenciais = verificar_credenciais(self.projeto.workspace)
        if not credenciais.ok:
            raise ErroCredenciais(credenciais.mensagem())
        valores = _ler_env(self.projeto.workspace)
        self.statement_timeout_ms = self._resolver_timeout(valores)
        parametros = self._parametros(valores)
        self._segredos = [str(v) for v in parametros.values() if v]
        self.trava.adquirir()
        try:
            self._conn = self._conectar_com_backoff(parametros)
            self._configurar_sessao()
        except BaseException:
            self.fechar()
            raise

    def fechar(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 - fechar nunca deve mascarar o erro original
                pass
            self._conn = None
        self.trava.liberar()

    # -- configuração

    def _resolver_timeout(self, valores: dict[str, str]) -> int:
        if self._statement_timeout_override is not None:
            return self._statement_timeout_override
        bruto = valores.get(VARIAVEL_TIMEOUT, "")
        if bruto:
            try:
                valor = int(bruto)
            except ValueError:
                valor = 0
            if valor <= 0:
                raise ErroCredenciais(
                    f"{VARIAVEL_TIMEOUT} no .env deve ser um inteiro positivo (milissegundos)."
                )
            return valor
        return self.projeto.statement_timeout_ms

    @staticmethod
    def _parametros(valores: dict[str, str]) -> dict[str, Any]:
        servico = valores.get(VARIAVEL_SERVICO) or os.environ.get(VARIAVEL_SERVICO, "").strip()
        parametros: dict[str, Any] = {"application_name": APPLICATION_NAME}
        if servico:
            parametros["service"] = servico
        mapa = {
            "PGHOST": "host",
            "PGPORT": "port",
            "PGDATABASE": "dbname",
            "PGUSER": "user",
            "PGPASSWORD": "password",
        }
        for variavel, chave in mapa.items():
            if valores.get(variavel):
                parametros[chave] = valores[variavel]
        return parametros

    def _conectar_com_backoff(self, parametros: dict[str, Any]) -> Any:
        total = len(self._backoff_s) + 1
        for tentativa in range(total):
            try:
                return self._conectar(autocommit=True, connect_timeout=15, **parametros)
            except psycopg.Error as erro:
                motivo, limite = _classificar_falha_de_conexao(erro)
                if not limite:
                    raise ErroConexao(f"Não foi possível conectar: {motivo}.") from None
                if tentativa == total - 1:
                    self.registro.evento("conexões esgotadas; tarefa abortada")
                    raise ErroConexao(
                        f"Não foi possível conectar: {motivo}. Esperei "
                        f"{', '.join(str(int(s)) for s in self._backoff_s)} s sem sucesso "
                        f"({total} tentativas); tarefa abortada."
                    ) from None
                espera = self._backoff_s[tentativa]
                self.registro.evento(f"sem conexões livres; nova tentativa em {espera:g} s")
                self._avisar(
                    f"Servidor sem conexões livres; aguardando {espera:g} s "
                    f"(tentativa {tentativa + 2} de {total})."
                )
                self._dormir(espera)
        raise ErroConexao("Não foi possível conectar.")  # inalcançável

    def _configurar_sessao(self) -> None:
        """Aplica as regras de sessão (fixas, internas) e confirma o modo somente leitura."""
        ajustes = (
            ("default_transaction_read_only", "on"),
            ("statement_timeout", str(self.statement_timeout_ms)),
            ("lock_timeout", str(self.projeto.lock_timeout_ms)),
            ("idle_in_transaction_session_timeout", "300000"),
            ("standard_conforming_strings", "on"),
            ("application_name", APPLICATION_NAME),
        )
        try:
            with self._conn.cursor() as cur:
                for nome, valor in ajustes:
                    cur.execute(SQL_SESSAO_SET_CONFIG, (nome, valor))
                cur.execute(SQL_SESSAO_CONFERIR_SOMENTE_LEITURA)
                linha = cur.fetchone()
            if not linha or str(linha[0]).lower() != "on":
                raise ErroConexao("A sessão não ficou somente leitura; conexão abortada.")
            self._conn.autocommit = False
            self._conn.read_only = True
        except psycopg.Error as erro:
            raise ErroConexao(
                f"Falha ao configurar a sessão (sqlstate {erro.sqlstate or 'desconhecido'})."
            ) from None
        self.registro.evento(
            f"sessão somente leitura; statement_timeout={self.statement_timeout_ms} ms; "
            f"lock_timeout={self.projeto.lock_timeout_ms} ms"
        )

    # -- execução

    def executar(self, sql: str, params: Sequence[Any] | Mapping[str, Any] | None = None) -> list[tuple[Any, ...]]:
        """Passa o SQL pelo guarda, registra e executa. Devolve as linhas (lista de tuplas)."""
        return self.executar_com_colunas(sql, params)[1]

    def executar_com_colunas(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] | None = None,
        *,
        tabelas_liberadas: Collection[str] | None = None,
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Como `executar`, mas devolve também os nomes das colunas do resultado.

        `tabelas_liberadas` ativa o modo de leitura de valores do guarda (D-023): só um
        SELECT de uma tabela dessa lista; é como `dominios ler` lê as tabelas de domínio.
        """
        try:
            guarda.verificar(sql, tabelas_liberadas)
        except SqlRejeitado as erro:
            self.registro.sql(sql, "rejeitado", str(erro))
            raise
        if self._conn is None:
            raise ErroConexao("Não há conexão aberta.")
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, params)
                colunas = [str(d[0]) for d in cur.description or []]
                linhas = cur.fetchall() if cur.description else []
        except psycopg.Error as erro:
            self._descartar_transacao()
            mensagem = self._mensagem_segura(erro)
            self.registro.sql(sql, "erro", mensagem)
            raise ErroExecucao(mensagem, getattr(erro, "sqlstate", None)) from None
        self._descartar_transacao()
        self.registro.sql(sql, "executado")
        return colunas, [tuple(linha) for linha in linhas]

    def _descartar_transacao(self) -> None:
        try:
            self._conn.rollback()  # nunca há o que confirmar: a sessão é somente leitura
        except psycopg.Error:
            pass

    def _mensagem_segura(self, erro: psycopg.Error) -> str:
        sqlstate = getattr(erro, "sqlstate", None) or "desconhecido"
        if sqlstate == "57014":
            return f"Consulta cancelada por tempo limite ({self.statement_timeout_ms} ms)."
        if sqlstate.startswith(("22", "23")):  # erros de dado podem citar o valor lido
            return f"Erro de dados do banco (sqlstate {sqlstate}); detalhe omitido por privacidade."
        diag = getattr(erro, "diag", None)
        primaria = (getattr(diag, "message_primary", None) or "erro do banco").strip()
        return f"Erro do banco (sqlstate {sqlstate}): {_sanear(primaria, self._segredos)}"
