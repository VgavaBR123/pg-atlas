#!/usr/bin/env python
"""Laço externo (Ralph loop) do pg-atlas: mantém o Claude Code trabalhando entre sessões.

Executar da pasta de trabalho (workspace):

    .\\.venv\\Scripts\\python pg-atlas\\tools\\loop.py

Cada volta é uma sessão nova do Claude Code em modo não interativo, que lê o estado em
disco e executa a próxima tarefa (ver `pg-atlas/docs/AUTONOMIA.md`, seção "Laço externo").

Versão do Claude Code conferida ao escrever este script: 2.1.309 (`claude --version`).
Flags usadas (conferidas com `claude --help`):

    -p / --print                    modo não interativo; o prompt vai pela entrada padrão
                                    (evita problemas de quebra de linha em .cmd no Windows)
    --output-format json            uma única saída JSON com is_error, subtype, result,
                                    total_cost_usd etc.
    --permission-prompts none       nada que pediria aprovação é aprovado: é negado
    --permission-mode <modo>        configurável por --modo-permissao (padrão: auto).
                                    Nunca bypassPermissions nem --dangerously-skip-permissions.

O processo roda com cwd no workspace, para que o `.claude/settings.json` do projeto valha.

Regras de segurança deste script:
- nunca lê nem imprime valores do `.env`: usa só `pg_atlas.conexao.verificar_credenciais`,
  que apenas testa se as variáveis estão preenchidas;
- nunca troca para a API quando o limite de uso da conta é atingido (gate G4);
- do texto da resposta do Claude grava no máximo 300 caracteres, no `loop.log`.

Modo `--simular`: mostra a próxima tarefa e o comando que seria executado, sem chamar o
Claude, sem gravar log e sem mexer em custos nem em pendências.

Para encerrar o loop, crie o arquivo `PARAR` na pasta de trabalho.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# ----------------------------------------------------------------- constantes

PROMPT_CONTINUACAO = """\
Você está no workspace do pg-atlas. Leia CLAUDE.md, depois atlas-eldados/_controle/ESTADO.md
e atlas-eldados/_controle/estado.json. Execute a PRÓXIMA tarefa pendente do plano
(pg-atlas/docs/PLANO.md) seguindo o laço da tarefa descrito em pg-atlas/docs/AUTONOMIA.md.
Não pergunte nada ao usuário: dúvidas vão para o agente decisor. Ao terminar a tarefa,
atualize ESTADO.md e estado.json, faça o commit e encerre a sessão.
"""

ESPERA_ENV_S = 10 * 60
ESPERA_LIMITE_PADRAO_S = 30 * 60
MARGEM_LIMITE_S = 60
ESPERA_MAXIMA_LIMITE_S = 24 * 3600
PASSO_ESPERA_S = 10
LIMITE_SEGUIDOS_G4 = 3
VOLTAS_SEM_AVANCO_G5 = 3
MAX_RESULTADO = 300
TIMEOUT_PADRAO_S = 3 * 3600
MAX_VOLTAS_PADRAO = 500

STATUS_ABERTOS = ("pendente", "em_andamento")
CABECALHO_PENDENCIAS = (
    "# Pendências humanas\n\n"
    "Gates que dependem do dono (ver `pg-atlas/docs/AUTONOMIA.md`). Para resolver, troque "
    "`ABERTA` por `RESOLVIDA` na linha correspondente. O loop confere este arquivo a cada "
    "volta.\n\n"
    "| Gate | Status | Aberto em | Descrição | O que fazer | O que continua enquanto isso |\n"
    "|---|---|---|---|---|---|\n"
)

# ----------------------------------------------------------------- tipos


@dataclass(frozen=True)
class Tarefa:
    fase: str
    id: str
    descricao: str
    depende_banco: bool
    status: str


@dataclass(frozen=True)
class ResultadoProcesso:
    codigo: int
    saida: str = ""
    erro: str = ""
    estourou_tempo: bool = False


Executor = Callable[..., ResultadoProcesso]


@dataclass
class Config:
    workspace: Path
    doc: Path
    max_voltas: int = MAX_VOLTAS_PADRAO
    modo_permissao: str = "auto"
    timeout_s: int = TIMEOUT_PADRAO_S
    claude: str = "claude"
    python: str = sys.executable

    @property
    def controle(self) -> Path:
        return self.doc / "_controle"

    @property
    def estado_json(self) -> Path:
        return self.controle / "estado.json"

    @property
    def pendencias(self) -> Path:
        return self.controle / "PENDENCIAS-HUMANAS.md"

    @property
    def log(self) -> Path:
        return self.controle / "loop.log"

    @property
    def parar(self) -> Path:
        return self.workspace / "PARAR"


def _agora_local() -> datetime:
    return datetime.now().astimezone()


def _imprimir(msg: str) -> None:
    print(msg, flush=True)


@dataclass
class Dependencias:
    """Tudo que toca o mundo externo, para os testes injetarem versões falsas."""

    executor: Executor
    dormir: Callable[[float], None] = time.sleep
    agora: Callable[[], datetime] = _agora_local
    env_ok: Callable[[Path], bool] | None = None
    imprimir: Callable[[str], None] = _imprimir

    def env_preenchido(self, workspace: Path) -> bool:
        return (self.env_ok or env_preenchido)(workspace)


# ----------------------------------------------------------------- funções puras: estado


def _chave_numerica(texto: str) -> tuple[int, ...]:
    return tuple(int(p) if p.isdigit() else 0 for p in str(texto).split("."))


def todas_concluidas(estado: Mapping[str, Any]) -> bool:
    """Verdadeiro se há fases e todas estão com status `concluida`."""
    fases = estado.get("fases") or {}
    return bool(fases) and all(f.get("status") == "concluida" for f in fases.values())


def proxima_tarefa(estado: Mapping[str, Any]) -> Tarefa | None:
    """Primeira tarefa não concluída, em ordem numérica de fase e tarefa, pulando `bloqueada`."""
    fases = estado.get("fases") or {}
    for chave_fase in sorted(fases, key=_chave_numerica):
        fase = fases[chave_fase]
        if fase.get("status") in ("concluida", "bloqueada"):
            continue
        tarefas = fase.get("tarefas") or {}
        for chave in sorted(tarefas, key=_chave_numerica):
            t = tarefas[chave]
            if t.get("status") in STATUS_ABERTOS:
                return Tarefa(
                    fase=str(chave_fase),
                    id=str(chave),
                    descricao=str(t.get("descricao", "")),
                    depende_banco=bool(t.get("depende_banco", False)),
                    status=str(t.get("status")),
                )
    return None


def resumo_estado(estado: Mapping[str, Any]) -> dict[str, str]:
    """Status de cada fase e de cada tarefa, para comparar antes e depois de uma volta."""
    resumo: dict[str, str] = {}
    for chave_fase, fase in (estado.get("fases") or {}).items():
        resumo[f"fase:{chave_fase}"] = str(fase.get("status"))
        for chave, t in (fase.get("tarefas") or {}).items():
            resumo[f"tarefa:{chave}"] = str(t.get("status"))
    return resumo


def houve_avanco(antes: Mapping[str, str], depois: Mapping[str, str]) -> bool:
    return dict(antes) != dict(depois)


# ----------------------------------------------------------------- funções puras: limite de uso

_PADROES_LIMITE = ("usage limit", "limit reached", "hit your limit", "resets")


def detectar_limite(texto: str) -> bool:
    """Reconhece a mensagem de limite de uso da conta."""
    minusculo = (texto or "").lower()
    return any(p in minusculo for p in _PADROES_LIMITE)


_RE_EPOCH = re.compile(r"\|\s*(\d{10,13})\b")
_RE_HORA = re.compile(
    r"resets?\s*(?:at\s*)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?:\s*\(([A-Za-z_/+\-0-9]+)\))?",
    re.IGNORECASE,
)
_RE_EM = re.compile(
    r"resets?\s+in\s+(?:(\d+)\s*h(?:ours?|rs?)?)?\s*(?:(\d+)\s*m(?:in(?:ute)?s?)?)?",
    re.IGNORECASE,
)


def extrair_liberacao(texto: str, agora: datetime) -> datetime | None:
    """Horário de liberação do limite, se a mensagem trouxer um. `agora` deve ter fuso.

    Formatos: epoch após `|`, "resets 3pm", "resets 3:30pm", "resets at 15:00",
    "resets 3pm (America/Sao_Paulo)" e "resets in 2 hours 30 minutes".
    """
    texto = texto or ""
    achou = _RE_EPOCH.search(texto)
    if achou:
        valor = int(achou.group(1))
        if len(achou.group(1)) == 13:
            valor //= 1000
        try:
            return datetime.fromtimestamp(valor, tz=agora.tzinfo)
        except (OverflowError, OSError, ValueError):
            return None

    achou = _RE_EM.search(texto)
    if achou and (achou.group(1) or achou.group(2)):
        horas = int(achou.group(1) or 0)
        minutos = int(achou.group(2) or 0)
        return agora + timedelta(hours=horas, minutes=minutos)

    achou = _RE_HORA.search(texto)
    if not achou:
        return None
    hora = int(achou.group(1))
    minuto = int(achou.group(2) or 0)
    periodo = (achou.group(3) or "").lower()
    if periodo:
        if not 1 <= hora <= 12:
            return None
        hora = hora % 12 + (12 if periodo == "pm" else 0)
    if hora > 23 or minuto > 59:
        return None
    referencia = agora
    if achou.group(4):
        try:
            from zoneinfo import ZoneInfo

            referencia = agora.astimezone(ZoneInfo(achou.group(4)))
        except Exception:  # noqa: BLE001 - fuso desconhecido ou sem tzdata: usa o local
            referencia = agora
    candidato = referencia.replace(hour=hora, minute=minuto, second=0, microsecond=0)
    if candidato <= referencia:
        candidato += timedelta(days=1)
    return candidato


def segundos_de_espera_limite(texto: str, agora: datetime) -> int:
    """Segundos até a liberação (com margem); 30 min se não houver horário razoável."""
    liberacao = extrair_liberacao(texto, agora)
    if liberacao is None:
        return ESPERA_LIMITE_PADRAO_S
    segundos = int((liberacao - agora).total_seconds()) + MARGEM_LIMITE_S
    if segundos <= 0 or segundos > ESPERA_MAXIMA_LIMITE_S:
        return ESPERA_LIMITE_PADRAO_S
    return segundos


# ----------------------------------------------------------------- funções puras: pendências


def _linhas_tabela(texto: str) -> list[list[str]]:
    linhas = []
    for linha in texto.splitlines():
        if linha.startswith("|"):
            linhas.append([c.strip() for c in linha.strip().strip("|").split("|")])
    return linhas


def gate_aberto(texto: str, gate: str) -> bool:
    """Existe linha do gate com status ABERTA."""
    return any(
        len(c) >= 2 and c[0] == gate and c[1].upper() == "ABERTA" for c in _linhas_tabela(texto)
    )


def adicionar_gate(
    texto: str,
    gate: str,
    data: str,
    descricao: str,
    o_que_fazer: str,
    continua: str,
) -> tuple[str, bool]:
    """Acrescenta a linha do gate na tabela. Não duplica se já houver uma ABERTA.

    Devolve (novo_texto, adicionou).
    """
    if gate_aberto(texto, gate):
        return texto, False

    def limpar(valor: str) -> str:
        return valor.replace("|", "/").replace("\n", " ").strip()

    nova = (
        f"| {gate} | ABERTA | {data} | {limpar(descricao)} | {limpar(o_que_fazer)} "
        f"| {limpar(continua)} |"
    )
    if not texto.strip():
        texto = CABECALHO_PENDENCIAS
    linhas = texto.rstrip("\n").split("\n")
    ultimo = max((i for i, ln in enumerate(linhas) if ln.startswith("|")), default=None)
    if ultimo is None:
        linhas.append("")
        linhas.extend(CABECALHO_PENDENCIAS.rstrip("\n").split("\n")[-2:])
        ultimo = len(linhas) - 1
    linhas.insert(ultimo + 1, nova)
    return "\n".join(linhas) + "\n", True


GATES = {
    "G4": (
        "Limite de uso da conta atingido {n} vezes seguidas. O loop segue esperando a "
        "liberação e não usa a API.",
        "Decidir se autoriza o uso da API (com teto). Sem decisão, nada muda: o loop continua "
        "esperando.",
        "O loop espera a liberação do limite e continua sozinho.",
    ),
    "G5": (
        "O loop parou: {n} voltas seguidas sem avanço no `estado.json` ({motivo}).",
        "Ler esta tabela e `_controle/loop.log`, corrigir a causa e marcar este gate como "
        "RESOLVIDA antes de reiniciar o loop.",
        "Nada; o loop está parado.",
    ),
}


# ----------------------------------------------------------------- funções puras: log e comando


def resumir_resultado(resultado: Any) -> str:
    """No máximo 300 caracteres do campo `result`, numa linha só."""
    if not isinstance(resultado, str):
        return ""
    return " ".join(resultado.split())[:MAX_RESULTADO]


def interpretar_saida(saida: str) -> dict[str, Any] | None:
    """Lê a saída JSON do `claude -p`; None se não for um objeto JSON."""
    try:
        dados = json.loads(saida)
    except (json.JSONDecodeError, TypeError):
        return None
    return dados if isinstance(dados, dict) else None


def montar_linha_log(
    *,
    volta: int,
    inicio: datetime,
    fim: datetime,
    tarefa: Tarefa | None,
    status: str,
    is_error: bool | None,
    saida: Mapping[str, Any] | None,
    avanco: bool,
    avisos: list[str],
) -> str:
    """Uma linha JSON por volta. Do texto da resposta, só os 300 primeiros caracteres."""
    custo = None
    subtype = None
    resultado = ""
    if saida:
        for chave in ("total_cost_usd", "cost_usd"):
            if isinstance(saida.get(chave), int | float):
                custo = saida[chave]
                break
        subtype = saida.get("subtype")
        resultado = resumir_resultado(saida.get("result"))
    registro = {
        "avanco": avanco,
        "avisos": avisos,
        "custo_usd": custo,
        "duracao_s": round((fim - inicio).total_seconds(), 1),
        "fase": tarefa.fase if tarefa else None,
        "fim": fim.isoformat(timespec="seconds"),
        "inicio": inicio.isoformat(timespec="seconds"),
        "is_error": is_error,
        "resultado": resultado,
        "status": status,
        "subtype": subtype,
        "tarefa": tarefa.id if tarefa else None,
        "volta": volta,
    }
    return json.dumps(registro, sort_keys=True, ensure_ascii=False)


def montar_comando(config: Config) -> list[str]:
    """Comando do Claude Code. O prompt NÃO entra aqui: vai pela entrada padrão."""
    return [
        config.claude,
        "-p",
        "--output-format",
        "json",
        "--permission-prompts",
        "none",
        "--permission-mode",
        config.modo_permissao,
    ]


# ----------------------------------------------------------------- efeitos: arquivos e processos


def _garantir_pacote() -> None:
    try:
        import pg_atlas  # noqa: F401
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def env_preenchido(workspace: Path) -> bool:
    """Só testa se as variáveis do `.env` estão preenchidas. Nunca devolve valores."""
    _garantir_pacote()
    from pg_atlas.conexao import verificar_credenciais

    return verificar_credenciais(workspace).ok


def _txt(valor: Any) -> str:
    if valor is None:
        return ""
    return valor.decode("utf-8", "replace") if isinstance(valor, bytes) else str(valor)


def executar_processo(
    comando: list[str],
    *,
    cwd: Path,
    entrada: str | None = None,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
) -> ResultadoProcesso:
    """Executor real (subprocess). Em estouro de tempo, o processo é encerrado."""
    try:
        r = subprocess.run(
            comando,
            cwd=cwd,
            input=entrada,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=dict(env) if env is not None else None,
        )
    except subprocess.TimeoutExpired as e:
        return ResultadoProcesso(124, _txt(e.stdout), _txt(e.stderr), estourou_tempo=True)
    except OSError as e:
        return ResultadoProcesso(127, "", f"falha ao executar {comando[0]}: {e}")
    return ResultadoProcesso(r.returncode, r.stdout or "", r.stderr or "")


def ler_estado(config: Config) -> dict[str, Any]:
    return json.loads(config.estado_json.read_text(encoding="utf-8"))


def ler_pendencias(config: Config) -> str:
    try:
        return config.pendencias.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def registrar_gate(config: Config, gate: str, hoje: str, **dados: Any) -> bool:
    """Grava o gate em PENDENCIAS-HUMANAS.md (sem duplicar um já ABERTO)."""
    descricao, fazer, continua = (m.format(**dados) for m in GATES[gate])
    novo, adicionou = adicionar_gate(ler_pendencias(config), gate, hoje, descricao, fazer, continua)
    if adicionou:
        config.controle.mkdir(parents=True, exist_ok=True)
        with open(config.pendencias, "w", encoding="utf-8", newline="\n") as f:
            f.write(novo)
    return adicionou


def gravar_log(config: Config, linha: str) -> None:
    config.controle.mkdir(parents=True, exist_ok=True)
    with open(config.log, "a", encoding="utf-8", newline="\n") as f:
        f.write(linha + "\n")


def _env_com_src() -> dict[str, str]:
    """Ambiente para o subprocesso de custos, garantindo `pg-atlas/src` no PYTHONPATH."""
    src = str(Path(__file__).resolve().parents[1] / "src")
    env = dict(os.environ)
    existente = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src + (os.pathsep + existente if existente else "")
    return env


def chamar_custos(
    config: Config, deps: Dependencias, args: list[str], avisos: list[str] | None = None
) -> bool:
    """Chama `python -m pg_atlas custos ...`. Falha vira aviso, nunca interrompe o loop."""
    comando = [config.python, "-m", "pg_atlas", "custos", *args, "--doc", str(config.doc)]
    try:
        r = deps.executor(
            comando, cwd=config.workspace, entrada=None, timeout=300, env=_env_com_src()
        )
        problema = None if r.codigo == 0 else f"custos {args[0]} falhou (código {r.codigo})"
    except Exception as e:  # noqa: BLE001 - o coletor nunca pode derrubar o loop
        problema = f"custos {args[0]}: exceção {type(e).__name__}"
    if problema:
        deps.imprimir(f"[aviso] {problema}")
        if avisos is not None:
            avisos.append(problema)
        return False
    return True


# ----------------------------------------------------------------- espera


def esperar(config: Config, deps: Dependencias, segundos: float) -> bool:
    """Dorme em passos de ~10 s conferindo `PARAR`. Devolve True se `PARAR` apareceu."""
    restante = float(segundos)
    while restante > 0:
        if config.parar.exists():
            return True
        passo = min(PASSO_ESPERA_S, restante)
        deps.dormir(passo)
        restante -= passo
    return config.parar.exists()


# ----------------------------------------------------------------- laço


def executar_loop(config: Config, deps: Dependencias) -> str:
    """Roda o laço externo. Devolve o motivo do encerramento.

    Motivos: `parar`, `concluido`, `max_voltas`, `sem_avanco` (G5), `sem_tarefa`,
    `estado_invalido`.
    """
    voltas = 0
    limites_seguidos = 0
    sem_avanco = 0
    g5_estava_aberta = gate_aberto(ler_pendencias(config), "G5")

    while True:
        # 1. PARAR
        if config.parar.exists():
            deps.imprimir("Arquivo PARAR encontrado: encerrando.")
            return "parar"

        # pendências: G5 resolvida pelo dono zera o contador de voltas sem avanço
        g5_aberta = gate_aberto(ler_pendencias(config), "G5")
        if g5_estava_aberta and not g5_aberta:
            deps.imprimir("G5 marcada como RESOLVIDA: contador de voltas sem avanço zerado.")
            sem_avanco = 0
        g5_estava_aberta = g5_aberta

        # 8. limite de voltas
        if voltas >= config.max_voltas:
            deps.imprimir(f"Limite de {config.max_voltas} voltas atingido: encerrando.")
            return "max_voltas"

        # 2. estado
        try:
            estado = ler_estado(config)
        except (OSError, json.JSONDecodeError) as e:
            deps.imprimir(f"Não foi possível ler {config.estado_json}: {e}")
            return "estado_invalido"
        if todas_concluidas(estado):
            deps.imprimir("Todas as fases concluídas: coletando e gerando o relatório de custos.")
            chamar_custos(config, deps, ["coletar"])
            chamar_custos(config, deps, ["relatorio"])
            return "concluido"

        # 3. próxima tarefa
        tarefa = proxima_tarefa(estado)
        if tarefa is None:
            deps.imprimir("Nenhuma tarefa disponível (restam só bloqueadas): encerrando.")
            registrar_gate(
                config,
                "G5",
                deps.agora().date().isoformat(),
                n=sem_avanco,
                motivo="não há tarefa disponível; restam apenas tarefas bloqueadas",
            )
            return "sem_tarefa"
        if tarefa.depende_banco and not deps.env_preenchido(config.workspace):
            deps.imprimir(
                f"Tarefa {tarefa.id} depende do banco e o .env não está preenchido: "
                "aguardando 10 minutos."
            )
            esperar(config, deps, ESPERA_ENV_S)
            continue

        # 4. volta
        voltas += 1
        antes = resumo_estado(estado)
        avisos: list[str] = []
        inicio = deps.agora()
        deps.imprimir(f"Volta {voltas}: tarefa {tarefa.id} ({tarefa.descricao}).")
        chamar_custos(
            config, deps, ["marcar-inicio", "--fase", tarefa.fase, "--tarefa", tarefa.id], avisos
        )
        r = deps.executor(
            montar_comando(config),
            cwd=config.workspace,
            entrada=PROMPT_CONTINUACAO,
            timeout=config.timeout_s,
            env=None,
        )
        fim = deps.agora()
        chamar_custos(
            config, deps, ["marcar-fim", "--fase", tarefa.fase, "--tarefa", tarefa.id], avisos
        )

        saida = interpretar_saida(r.saida)
        is_error = bool(saida.get("is_error")) if saida else None
        falhou = r.estourou_tempo or r.codigo != 0 or saida is None or bool(is_error)
        texto_diag = " ".join(
            p
            for p in (str(saida.get("result", "")) if saida else r.saida, r.erro)
            if p
        )

        if r.estourou_tempo:
            status = "timeout"
        elif falhou and detectar_limite(texto_diag):
            status = "limite"
        elif falhou:
            status = "erro"
        else:
            status = "ok"

        try:
            depois = resumo_estado(ler_estado(config))
        except (OSError, json.JSONDecodeError):
            depois = antes
            avisos.append("estado.json ilegível após a volta")
        avancou = houve_avanco(antes, depois)

        # 5. registro da volta
        gravar_log(
            config,
            montar_linha_log(
                volta=voltas,
                inicio=inicio,
                fim=fim,
                tarefa=tarefa,
                status=status,
                is_error=is_error,
                saida=saida,
                avanco=avancou,
                avisos=avisos,
            ),
        )

        # 6. limite de uso: espera e tenta a mesma tarefa; não conta como volta sem avanço
        if status == "limite":
            limites_seguidos += 1
            if limites_seguidos >= LIMITE_SEGUIDOS_G4 and registrar_gate(
                config, "G4", deps.agora().date().isoformat(), n=limites_seguidos
            ):
                deps.imprimir("Gate G4 registrado em PENDENCIAS-HUMANAS.md.")
            segundos = segundos_de_espera_limite(texto_diag, deps.agora())
            deps.imprimir(
                f"Limite de uso atingido ({limites_seguidos} seguidos): aguardando {segundos} s."
            )
            esperar(config, deps, segundos)
            continue
        limites_seguidos = 0

        # 7. sem avanço
        if avancou:
            sem_avanco = 0
            continue
        sem_avanco += 1
        deps.imprimir(f"Volta sem avanço ({sem_avanco}/{VOLTAS_SEM_AVANCO_G5}).")
        if sem_avanco >= VOLTAS_SEM_AVANCO_G5:
            registrar_gate(
                config,
                "G5",
                deps.agora().date().isoformat(),
                n=sem_avanco,
                motivo=f"última tarefa: {tarefa.id}, status da volta: {status}",
            )
            deps.imprimir("Gate G5 registrado em PENDENCIAS-HUMANAS.md: encerrando.")
            return "sem_avanco"


# ----------------------------------------------------------------- simulação e CLI


def simular(config: Config, deps: Dependencias) -> int:
    """Mostra o que a próxima volta faria, sem executar nada."""
    p = deps.imprimir
    p(f"Workspace: {config.workspace}")
    p(f"Documentação: {config.doc}")
    if config.parar.exists():
        p("Arquivo PARAR existe: o loop encerraria agora.")
        return 0
    try:
        estado = ler_estado(config)
    except (OSError, json.JSONDecodeError) as e:
        p(f"Não foi possível ler {config.estado_json}: {e}")
        return 2
    if todas_concluidas(estado):
        p("Todas as fases concluídas: o loop rodaria `custos coletar` e `custos relatorio`.")
        return 0
    tarefa = proxima_tarefa(estado)
    if tarefa is None:
        p("Nenhuma tarefa disponível (restam só bloqueadas).")
        return 0
    p(f"Próxima tarefa: {tarefa.id} - {tarefa.descricao} (fase {tarefa.fase}, {tarefa.status})")
    p(f"Depende do banco: {'sim' if tarefa.depende_banco else 'não'}")
    if tarefa.depende_banco and not deps.env_preenchido(config.workspace):
        p(".env ausente ou incompleto: o loop esperaria 10 minutos e voltaria ao passo 1.")
    else:
        p("Comando (o prompt vai pela entrada padrão):")
        p("  " + " ".join(montar_comando(config)))
        p(f"  cwd={config.workspace}  timeout={config.timeout_s}s")
        p("Prompt de continuação:")
        for linha in PROMPT_CONTINUACAO.splitlines():
            p("  | " + linha)
        p(
            "Custos: marcar-inicio e marcar-fim com "
            f"--fase {tarefa.fase} --tarefa {tarefa.id} --doc {config.doc}"
        )
    p("Simulação: nada foi executado, gravado ou marcado.")
    return 0


def doc_padrao(workspace: Path) -> Path:
    """Único repositório irmão de pg-atlas com `_controle/estado.json`; senão `atlas-eldados`."""
    candidatos = sorted(
        p
        for p in workspace.iterdir()
        if p.is_dir() and p.name != "pg-atlas" and (p / "_controle" / "estado.json").is_file()
    )
    return candidatos[0] if len(candidatos) == 1 else workspace / "atlas-eldados"


def criar_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Laço externo do pg-atlas: roda o Claude Code em voltas até o plano acabar.",
        epilog="Crie o arquivo PARAR na pasta de trabalho para encerrar o loop.",
    )
    ap.add_argument(
        "--doc",
        type=Path,
        help="repositório de documentação (padrão: o irmão de pg-atlas com _controle/estado.json)",
    )
    ap.add_argument(
        "--max-voltas", type=int, default=MAX_VOLTAS_PADRAO, help="limite de voltas (padrão: 500)"
    )
    ap.add_argument(
        "--modo-permissao",
        default="auto",
        choices=["acceptEdits", "auto", "manual", "dontAsk", "plan"],
        help="valor de --permission-mode do Claude Code (padrão: auto; "
        "bypassPermissions não é aceito)",
    )
    ap.add_argument(
        "--timeout-h",
        type=float,
        default=TIMEOUT_PADRAO_S / 3600,
        help="timeout por volta, em horas (padrão: 3)",
    )
    ap.add_argument(
        "--workspace", type=Path, help="pasta de trabalho (padrão: a que contém pg-atlas/tools)"
    )
    ap.add_argument(
        "--simular",
        action="store_true",
        help="mostra a próxima tarefa e o comando, sem executar o Claude nem gravar nada",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    for fluxo in (sys.stdout, sys.stderr):  # acentos corretos mesmo com saída redirecionada
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8", errors="replace")
    args = criar_parser().parse_args(argv)
    workspace = (args.workspace or Path(__file__).resolve().parents[2]).resolve()
    doc = (args.doc or doc_padrao(workspace)).resolve()
    config = Config(
        workspace=workspace,
        doc=doc,
        max_voltas=args.max_voltas,
        modo_permissao=args.modo_permissao,
        timeout_s=int(args.timeout_h * 3600),
        claude=shutil.which("claude") or "claude",
    )
    deps = Dependencias(executor=executar_processo)
    if args.simular:
        return simular(config, deps)
    motivo = executar_loop(config, deps)
    deps.imprimir(f"Encerrado: {motivo}.")
    return 1 if motivo in ("sem_avanco", "sem_tarefa", "estado_invalido") else 0


if __name__ == "__main__":
    sys.exit(main())
