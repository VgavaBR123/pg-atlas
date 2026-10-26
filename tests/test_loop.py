"""Testes offline do laço externo (`tools/loop.py`): sem Claude real e sem dormir de verdade."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

CAMINHO = Path(__file__).resolve().parents[1] / "tools" / "loop.py"
_spec = importlib.util.spec_from_file_location("pg_atlas_loop", CAMINHO)
assert _spec and _spec.loader
loop = importlib.util.module_from_spec(_spec)
sys.modules["pg_atlas_loop"] = loop
_spec.loader.exec_module(loop)

FUSO = timezone(timedelta(hours=-3))
AGORA = datetime(2026, 10, 23, 10, 0, 0, tzinfo=FUSO)

PENDENCIAS = """\
# Pendências humanas

Texto.

| Gate | Status | Aberto em | Descrição | O que fazer | O que continua enquanto isso |
|---|---|---|---|---|---|
| G1 | RESOLVIDA | 2026-10-23 | `.env` ok. | Nada. | — |
"""


def estado_basico() -> dict:
    return {
        "fases": {
            "0": {
                "status": "em_andamento",
                "tarefas": {
                    "0.1": {"descricao": "a", "depende_banco": False, "status": "concluida"},
                    "0.2": {"descricao": "b", "depende_banco": False, "status": "pendente"},
                    "0.10": {"descricao": "c", "depende_banco": False, "status": "pendente"},
                },
            },
            "1": {
                "status": "pendente",
                "tarefas": {
                    "1.1": {"descricao": "d", "depende_banco": True, "status": "pendente"},
                },
            },
        }
    }


class Ambiente:
    """Workspace e documentação falsos em pasta temporária, com executor e relógio falsos."""

    def __init__(self, tmp: Path, estado: dict | None = None, env_ok: bool = True) -> None:
        self.workspace = tmp / "ws"
        self.doc = self.workspace / "atlas-eldados"
        (self.doc / "_controle").mkdir(parents=True)
        self.config = loop.Config(workspace=self.workspace, doc=self.doc, python="py")
        self.estado = estado if estado is not None else estado_basico()
        self.gravar_estado()
        self.config.pendencias.write_text(PENDENCIAS, encoding="utf-8")
        self.env_ok = env_ok
        self.chamadas: list[list[str]] = []
        self.entradas: list[str | None] = []
        self.respostas: list = []  # callables (ambiente) -> ResultadoProcesso
        self.custos_falha = False
        self.sonos: list[float] = []
        self.parar_apos_sonos: int | None = None
        self.mensagens: list[str] = []
        self.relogio = AGORA

    def gravar_estado(self) -> None:
        self.config.estado_json.write_text(json.dumps(self.estado), encoding="utf-8")

    def concluir(self, tarefa: str, fase: str = "0") -> None:
        self.estado["fases"][fase]["tarefas"][tarefa]["status"] = "concluida"
        self.gravar_estado()

    # injeções
    def executor(self, comando, *, cwd, entrada=None, timeout=None, env=None):
        self.chamadas.append(list(comando))
        self.entradas.append(entrada)
        if "custos" in comando:
            return loop.ResultadoProcesso(1 if self.custos_falha else 0)
        resposta = self.respostas.pop(0) if self.respostas else self.sem_resposta
        return resposta(self)

    @staticmethod
    def sem_resposta(_amb):
        return loop.ResultadoProcesso(0, ok_json("nada"))

    def dormir(self, s: float) -> None:
        self.sonos.append(s)
        if self.parar_apos_sonos is not None and len(self.sonos) >= self.parar_apos_sonos:
            (self.workspace / "PARAR").write_text("", encoding="utf-8")

    def deps(self) -> loop.Dependencias:
        return loop.Dependencias(
            executor=self.executor,
            dormir=self.dormir,
            agora=lambda: self.relogio,
            env_ok=lambda _w: self.env_ok,
            imprimir=self.mensagens.append,
        )

    def rodar(self) -> str:
        return loop.executar_loop(self.config, self.deps())

    def chamadas_claude(self) -> list[list[str]]:
        return [c for c in self.chamadas if "custos" not in c]

    def linhas_log(self) -> list[dict]:
        if not self.config.log.exists():
            return []
        return [json.loads(x) for x in self.config.log.read_text(encoding="utf-8").splitlines()]


def ok_json(resultado: str = "feito", custo: float = 1.25) -> str:
    return json.dumps(
        {"is_error": False, "subtype": "success", "result": resultado, "total_cost_usd": custo}
    )


def resp_ok(avanca: str | None = None, fase: str = "0"):
    def f(amb: Ambiente):
        if avanca:
            amb.concluir(avanca, fase)
        return loop.ResultadoProcesso(0, ok_json())

    return f


def resp_limite(texto: str = "Claude AI usage limit reached"):
    def f(_amb):
        return loop.ResultadoProcesso(
            1, json.dumps({"is_error": True, "subtype": "success", "result": texto})
        )

    return f


@pytest.fixture
def amb(tmp_path: Path) -> Ambiente:
    return Ambiente(tmp_path)


# ------------------------------------------------------------------ escolha da tarefa


def test_proxima_tarefa_ordem_numerica_e_pula_concluida() -> None:
    t = loop.proxima_tarefa(estado_basico())
    assert t is not None and t.id == "0.2"  # 0.2 vem antes de 0.10


def test_proxima_tarefa_pula_bloqueada() -> None:
    estado = estado_basico()
    estado["fases"]["0"]["tarefas"]["0.2"]["status"] = "bloqueada"
    assert loop.proxima_tarefa(estado).id == "0.10"
    estado["fases"]["0"]["tarefas"]["0.10"]["status"] = "bloqueada"
    t = loop.proxima_tarefa(estado)
    assert t.id == "1.1" and t.fase == "1" and t.depende_banco


def test_proxima_tarefa_em_andamento_conta_e_nada_sobrando() -> None:
    estado = estado_basico()
    estado["fases"]["0"]["tarefas"]["0.2"]["status"] = "em_andamento"
    assert loop.proxima_tarefa(estado).id == "0.2"
    for fase in estado["fases"].values():
        for t in fase["tarefas"].values():
            t["status"] = "bloqueada"
    assert loop.proxima_tarefa(estado) is None


# ------------------------------------------------------------------ PARAR, concluído, .env


def test_parar_encerra_sem_chamar_claude(amb: Ambiente) -> None:
    (amb.workspace / "PARAR").write_text("", encoding="utf-8")
    assert amb.rodar() == "parar"
    assert amb.chamadas == []
    assert amb.linhas_log() == []


def test_todas_concluidas_gera_relatorio_de_custos(amb: Ambiente) -> None:
    for fase in amb.estado["fases"].values():
        fase["status"] = "concluida"
    amb.gravar_estado()
    assert amb.rodar() == "concluido"
    sub = [c[c.index("custos") + 1] for c in amb.chamadas]
    assert sub == ["coletar", "relatorio"]
    assert amb.chamadas_claude() == []


def test_espera_env_com_tarefa_de_banco(amb: Ambiente) -> None:
    amb.estado["fases"]["0"]["status"] = "concluida"
    for t in amb.estado["fases"]["0"]["tarefas"].values():
        t["status"] = "concluida"
    amb.gravar_estado()
    amb.env_ok = False
    amb.parar_apos_sonos = 60  # 10 min em passos de 10 s = 60 sonos; PARAR chega ao fim da espera
    assert amb.rodar() == "parar"
    assert amb.chamadas == []
    assert sum(amb.sonos) == 600
    assert max(amb.sonos) <= 10


def test_espera_env_confere_parar_com_frequencia(amb: Ambiente) -> None:
    for t in amb.estado["fases"]["0"]["tarefas"].values():
        t["status"] = "concluida"
    amb.gravar_estado()
    amb.env_ok = False
    amb.parar_apos_sonos = 2
    assert amb.rodar() == "parar"
    assert len(amb.sonos) == 2  # parou após ~20 s, não após 10 min


def test_tarefa_sem_banco_ignora_env(amb: Ambiente) -> None:
    amb.env_ok = False
    amb.respostas = [resp_ok("0.2"), resp_ok("0.10"), resp_ok("1.1", "1")]
    amb.config.max_voltas = 1
    assert amb.rodar() == "max_voltas"
    assert len(amb.chamadas_claude()) == 1


# ------------------------------------------------------------------ limite de uso


@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("Claude AI usage limit reached|1760000000", True),
        ("You've hit your limit - resets 3pm", True),
        ("5-hour limit reached. Resets at 15:00", True),
        ("Erro qualquer de rede", False),
        ("", False),
    ],
)
def test_detectar_limite(texto: str, esperado: bool) -> None:
    assert loop.detectar_limite(texto) is esperado


def test_extrair_liberacao_formatos() -> None:
    ex = loop.extrair_liberacao
    assert ex("usage limit reached|1760000000", AGORA) == datetime.fromtimestamp(1760000000, tz=FUSO)
    assert ex("usage limit reached|1760000000123", AGORA) == datetime.fromtimestamp(
        1760000000, tz=FUSO
    )
    assert ex("hit your limit, resets 3pm", AGORA) == AGORA.replace(hour=15)
    assert ex("resets 3:30pm", AGORA) == AGORA.replace(hour=15, minute=30)
    assert ex("resets at 15:00", AGORA) == AGORA.replace(hour=15)
    # hora já passada hoje: amanhã
    assert ex("resets 9am", AGORA) == AGORA.replace(hour=9) + timedelta(days=1)
    assert ex("resets 12am", AGORA) == AGORA.replace(hour=0) + timedelta(days=1)
    assert ex("resets in 2 hours 30 minutes", AGORA) == AGORA + timedelta(hours=2, minutes=30)
    assert ex("limit reached", AGORA) is None
    assert ex("resets 99pm", AGORA) is None


def test_segundos_de_espera_limite() -> None:
    f = loop.segundos_de_espera_limite
    assert f("resets 3pm", AGORA) == 5 * 3600 + loop.MARGEM_LIMITE_S
    assert f("usage limit reached", AGORA) == 30 * 60
    passado = int((AGORA - timedelta(hours=1)).timestamp())
    assert f(f"limit reached|{passado}", AGORA) == 30 * 60
    longe = int((AGORA + timedelta(days=5)).timestamp())
    assert f(f"limit reached|{longe}", AGORA) == 30 * 60


def test_limite_espera_horario_e_tenta_a_mesma_tarefa(amb: Ambiente) -> None:
    amb.respostas = [resp_limite("You've hit your limit - resets 3pm"), resp_ok("0.2")]
    amb.config.max_voltas = 2
    assert amb.rodar() == "max_voltas"
    assert sum(amb.sonos) == 5 * 3600 + loop.MARGEM_LIMITE_S
    assert max(amb.sonos) <= 10
    log = amb.linhas_log()
    assert [x["status"] for x in log] == ["limite", "ok"]
    assert [x["tarefa"] for x in log] == ["0.2", "0.2"]
    assert "G4" not in amb.config.pendencias.read_text(encoding="utf-8")


def test_limite_sem_horario_espera_30_minutos(amb: Ambiente) -> None:
    amb.respostas = [resp_limite("Claude AI usage limit reached")]
    amb.config.max_voltas = 1
    amb.rodar()
    assert sum(amb.sonos) == 1800


def test_g4_apos_tres_limites_seguidos_uma_vez_so(amb: Ambiente) -> None:
    amb.respostas = [resp_limite()] * 5
    amb.config.max_voltas = 5
    assert amb.rodar() == "max_voltas"
    texto = amb.config.pendencias.read_text(encoding="utf-8")
    linhas_g4 = [x for x in texto.splitlines() if x.startswith("| G4 ")]
    assert len(linhas_g4) == 1
    assert "| ABERTA |" in linhas_g4[0]
    assert all(x["status"] == "limite" for x in amb.linhas_log())
    # limites não contam como voltas sem avanço
    assert "| G5 " not in texto


def test_dois_limites_nao_abrem_g4_e_contador_zera(amb: Ambiente) -> None:
    amb.respostas = [resp_limite(), resp_limite(), resp_ok("0.2"), resp_limite(), resp_limite()]
    amb.config.max_voltas = 5
    amb.rodar()
    assert "| G4 " not in amb.config.pendencias.read_text(encoding="utf-8")


def test_limite_na_saida_nao_json_e_stderr(amb: Ambiente) -> None:
    amb.respostas = [lambda _a: loop.ResultadoProcesso(1, "", "Usage limit reached|1760000000")]
    amb.config.max_voltas = 1
    amb.rodar()
    assert amb.linhas_log()[0]["status"] == "limite"


# ------------------------------------------------------------------ sem avanço / G5


def test_g5_apos_tres_voltas_sem_avanco(amb: Ambiente) -> None:
    amb.respostas = [resp_ok()] * 10
    assert amb.rodar() == "sem_avanco"
    assert len(amb.chamadas_claude()) == 3
    texto = amb.config.pendencias.read_text(encoding="utf-8")
    linhas = [x for x in texto.splitlines() if x.startswith("| G5 ")]
    assert len(linhas) == 1 and "| ABERTA |" in linhas[0]
    # as linhas antigas continuam intactas e a nova está dentro da tabela
    assert "| G1 | RESOLVIDA |" in texto


def test_avanco_zera_contador_sem_avanco(amb: Ambiente) -> None:
    amb.respostas = [resp_ok(), resp_ok(), resp_ok("0.2"), resp_ok(), resp_ok(), resp_ok("0.10")]
    amb.env_ok = False  # a tarefa 1.1 (banco) faz o loop esperar; PARAR encerra
    amb.parar_apos_sonos = 1
    assert amb.rodar() == "parar"
    assert len(amb.chamadas_claude()) == 6
    assert "| G5 " not in amb.config.pendencias.read_text(encoding="utf-8")


def test_g5_resolvida_zera_contador(amb: Ambiente) -> None:
    amb.respostas = [resp_ok()] * 20
    assert amb.rodar() == "sem_avanco"
    assert len(amb.chamadas_claude()) == 3

    # o dono resolve; nova execução do loop começa vendo a G5 ABERTA e depois RESOLVIDA
    amb.chamadas.clear()
    deps = amb.deps()
    resolvida = {"feito": False}
    original = amb.executor

    def executor(comando, **kw):
        r = original(comando, **kw)
        if "custos" not in comando and len(amb.chamadas_claude()) == 2 and not resolvida["feito"]:
            texto = amb.config.pendencias.read_text(encoding="utf-8")
            amb.config.pendencias.write_text(
                texto.replace("| G5 | ABERTA |", "| G5 | RESOLVIDA |"), encoding="utf-8"
            )
            resolvida["feito"] = True
        return r

    deps.executor = executor
    assert loop.executar_loop(amb.config, deps) == "sem_avanco"
    # sem a resolução pararia na 3ª; com ela zera após a 2ª e precisa de mais 3 voltas
    assert len(amb.chamadas_claude()) == 5
    texto = amb.config.pendencias.read_text(encoding="utf-8")
    assert sum(1 for x in texto.splitlines() if x.startswith("| G5 ")) == 2


def test_sem_tarefa_disponivel_registra_g5(amb: Ambiente) -> None:
    for fase in amb.estado["fases"].values():
        for t in fase["tarefas"].values():
            t["status"] = "bloqueada"
    amb.gravar_estado()
    assert amb.rodar() == "sem_tarefa"
    assert gate_na_pendencia(amb, "G5")


def gate_na_pendencia(amb: Ambiente, gate: str) -> bool:
    return loop.gate_aberto(amb.config.pendencias.read_text(encoding="utf-8"), gate)


# ------------------------------------------------------------------ max-voltas e comando


def test_max_voltas(amb: Ambiente) -> None:
    amb.respostas = [resp_ok("0.2"), resp_ok("0.10"), resp_ok("1.1", "1")]
    amb.config.max_voltas = 2
    assert amb.rodar() == "max_voltas"
    assert len(amb.chamadas_claude()) == 2
    assert [x["tarefa"] for x in amb.linhas_log()] == ["0.2", "0.10"]


def test_comando_do_claude_e_prompt_exato(amb: Ambiente) -> None:
    amb.respostas = [resp_ok("0.2")]
    amb.config.max_voltas = 1
    amb.config.claude = "claude"
    amb.rodar()
    cmd = amb.chamadas_claude()[0]
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--permission-prompts") + 1] == "none"
    assert cmd[cmd.index("--permission-mode") + 1] == "auto"
    assert "bypassPermissions" not in cmd
    assert "--dangerously-skip-permissions" not in cmd
    entrada = [e for c, e in zip(amb.chamadas, amb.entradas, strict=True) if "custos" not in c][0]
    assert entrada == loop.PROMPT_CONTINUACAO
    assert "Não pergunte nada ao usuário" in entrada


def test_modo_permissao_rejeita_bypass() -> None:
    with pytest.raises(SystemExit):
        loop.criar_parser().parse_args(["--modo-permissao", "bypassPermissions"])
    assert loop.criar_parser().parse_args(["--modo-permissao", "plan"]).modo_permissao == "plan"


# ------------------------------------------------------------------ custos


def test_custos_marcar_inicio_e_fim(amb: Ambiente) -> None:
    amb.respostas = [resp_ok("0.2")]
    amb.config.max_voltas = 1
    amb.rodar()
    custos = [c for c in amb.chamadas if "custos" in c]
    assert [c[c.index("custos") + 1] for c in custos] == ["marcar-inicio", "marcar-fim"]
    for c in custos:
        assert c[c.index("--fase") + 1] == "0"
        assert c[c.index("--tarefa") + 1] == "0.2"
        assert c[c.index("--doc") + 1] == str(amb.doc)
        assert c[:3] == ["py", "-m", "pg_atlas"]


def test_custos_com_falha_vira_aviso_e_segue(amb: Ambiente) -> None:
    amb.custos_falha = True
    amb.respostas = [resp_ok("0.2")]
    amb.config.max_voltas = 1
    assert amb.rodar() == "max_voltas"
    log = amb.linhas_log()[0]
    assert len(log["avisos"]) == 2
    assert any("[aviso]" in m for m in amb.mensagens)


# ------------------------------------------------------------------ loop.log


def test_linha_do_loop_log(amb: Ambiente) -> None:
    longo = "x" * 1000
    amb.respostas = [
        lambda a: (a.concluir("0.2"), loop.ResultadoProcesso(0, ok_json(longo, 2.5)))[1]
    ]
    amb.config.max_voltas = 1
    amb.rodar()
    bruto = amb.config.log.read_text(encoding="utf-8")
    assert bruto.count("\n") == 1 and bruto.endswith("\n")
    linha = json.loads(bruto)
    assert linha["tarefa"] == "0.2" and linha["fase"] == "0" and linha["volta"] == 1
    assert linha["status"] == "ok" and linha["is_error"] is False
    assert linha["custo_usd"] == 2.5
    assert linha["avanco"] is True
    assert linha["duracao_s"] == 0.0
    assert linha["inicio"] == AGORA.isoformat(timespec="seconds")
    assert len(linha["resultado"]) == 300  # nunca a resposta inteira
    assert longo not in bruto


def test_log_sem_custo_e_timeout(amb: Ambiente) -> None:
    amb.respostas = [lambda _a: loop.ResultadoProcesso(124, "", "", estourou_tempo=True)]
    amb.config.max_voltas = 1
    amb.rodar()
    linha = amb.linhas_log()[0]
    assert linha["status"] == "timeout"
    assert linha["custo_usd"] is None and linha["is_error"] is None


def test_log_saida_nao_json_nao_grava_texto(amb: Ambiente) -> None:
    amb.respostas = [lambda _a: loop.ResultadoProcesso(1, "lixo " * 500, "boom")]
    amb.config.max_voltas = 1
    amb.rodar()
    linha = amb.linhas_log()[0]
    assert linha["status"] == "erro" and linha["resultado"] == ""


# ------------------------------------------------------------------ pendências (funções puras)


def test_adicionar_gate_formato_e_sem_duplicar() -> None:
    novo, adicionou = loop.adicionar_gate(PENDENCIAS, "G4", "2026-10-23", "desc", "fazer", "segue")
    assert adicionou
    assert novo.endswith("| G4 | ABERTA | 2026-10-23 | desc | fazer | segue |\n")
    assert novo.startswith(PENDENCIAS.rstrip("\n"))
    outro, de_novo = loop.adicionar_gate(novo, "G4", "2026-10-24", "x", "y", "z")
    assert not de_novo and outro == novo
    # gate RESOLVIDA antigo não impede um novo
    resolvido = novo.replace("| G4 | ABERTA |", "| G4 | RESOLVIDA |")
    again, ok = loop.adicionar_gate(resolvido, "G4", "2026-10-25", "x", "y", "z")
    assert ok and again.count("| G4 |") == 2


def test_adicionar_gate_em_arquivo_vazio_cria_tabela() -> None:
    novo, ok = loop.adicionar_gate("", "G5", "2026-10-23", "d", "f", "c")
    assert ok and "| Gate | Status |" in novo and "| G5 | ABERTA |" in novo


def test_gate_aberto() -> None:
    assert not loop.gate_aberto(PENDENCIAS, "G1")
    assert loop.gate_aberto(PENDENCIAS.replace("| G1 | RESOLVIDA", "| G1 | ABERTA"), "G1")


# ------------------------------------------------------------------ simulação e padrões


def test_simular_nao_executa_nada(amb: Ambiente) -> None:
    deps = amb.deps()
    assert loop.simular(amb.config, deps) == 0
    saida = "\n".join(amb.mensagens)
    assert "Próxima tarefa: 0.2" in saida
    assert "--permission-prompts none" in saida
    assert amb.chamadas == []
    assert not amb.config.log.exists()


def test_doc_padrao(tmp_path: Path) -> None:
    (tmp_path / "pg-atlas").mkdir()
    assert loop.doc_padrao(tmp_path) == tmp_path / "atlas-eldados"
    outro = tmp_path / "meu-banco" / "_controle"
    outro.mkdir(parents=True)
    (outro / "estado.json").write_text("{}", encoding="utf-8")
    assert loop.doc_padrao(tmp_path) == tmp_path / "meu-banco"


# ------------------------------------------------------------------ executor pelo SDK


class ResultMessage:  # o nome da classe importa: o executor reconhece pelo tipo
    def __init__(self, **kw) -> None:
        base = dict(
            subtype="success",
            duration_ms=1200,
            duration_api_ms=900,
            is_error=False,
            num_turns=7,
            session_id="sessao-1",
            total_cost_usd=0.42,
            usage={"input_tokens": 10, "output_tokens": 5},
            result="feito",
        )
        base.update(kw)
        self.__dict__.update(base)


def query_falsa(*mensagens, depois=None):
    capturado: dict = {}

    async def query(*, prompt, options):
        capturado["prompt"] = prompt
        capturado["options"] = options
        for m in mensagens:
            yield m
        if depois:
            await depois()

    return query, capturado


def sdk(query, **kw):
    kw.setdefault("timeout", 5)
    return loop.executar_via_sdk("oi", cwd=Path("."), modo_permissao="auto", query=query, **kw)


def test_sdk_converte_resultado_em_json_do_claude_p() -> None:
    pytest.importorskip("claude_agent_sdk")
    query, cap = query_falsa(object(), ResultMessage())
    r = sdk(query, modelo="sonnet")
    assert r.codigo == 0 and not r.estourou_tempo and r.erro == ""
    dados = loop.interpretar_saida(r.saida)
    assert dados is not None
    for chave in (
        "is_error",
        "subtype",
        "result",
        "total_cost_usd",
        "num_turns",
        "duration_ms",
        "session_id",
        "usage",
    ):
        assert chave in dados
    assert dados["total_cost_usd"] == 0.42 and dados["is_error"] is False
    assert dados["result"] == "feito" and dados["num_turns"] == 7
    assert cap["prompt"] == "oi"
    op = cap["options"]
    assert op.permission_mode == "auto" and op.model == "sonnet"
    assert op.setting_sources == ["user", "project", "local"]
    assert op.env["CLAUDE_AGENT_SDK_CLIENT_APP"].startswith("pg-atlas-loop/")
    assert op.can_use_tool is not None


def test_sdk_erro_do_agente_mantem_is_error() -> None:
    pytest.importorskip("claude_agent_sdk")
    query, _ = query_falsa(
        ResultMessage(is_error=True, subtype="error_max_turns", result=None, errors=["x"])
    )
    dados = loop.interpretar_saida(sdk(query).saida)
    assert dados["is_error"] is True and dados["subtype"] == "error_max_turns"
    assert dados["result"] == "x"


def test_sdk_estouro_de_tempo() -> None:
    pytest.importorskip("claude_agent_sdk")
    import asyncio

    async def dormir():
        await asyncio.sleep(10)

    query, _ = query_falsa(depois=dormir)
    r = sdk(query, timeout=0.05)
    assert r.estourou_tempo and r.codigo == 124


def test_sdk_excecao_vira_codigo_diferente_de_zero() -> None:
    pytest.importorskip("claude_agent_sdk")

    async def query(*, prompt, options):
        raise RuntimeError("falhou " + "x" * 1000)
        yield  # pragma: no cover

    r = sdk(query)
    assert r.codigo != 0 and not r.estourou_tempo
    assert r.erro.startswith("RuntimeError: falhou") and len(r.erro) <= 300


def test_sdk_sem_result_message() -> None:
    pytest.importorskip("claude_agent_sdk")
    query, _ = query_falsa(object())
    r = sdk(query)
    assert r.codigo != 0 and "ResultMessage" in r.erro


def test_sdk_can_use_tool_sempre_nega() -> None:
    pytest.importorskip("claude_agent_sdk")
    import asyncio

    for nome in ("Bash", "Write", "Read"):
        r = asyncio.run(loop._negar_permissao(nome, {"x": 1}, None))
        assert r.behavior == "deny" and r.message


def test_parser_aceita_executor_e_modelo() -> None:
    p = loop.criar_parser()
    a = p.parse_args([])
    assert a.executor == "cli" and a.modelo is None
    a = p.parse_args(["--executor", "sdk", "--modelo", "opus"])
    assert a.executor == "sdk" and a.modelo == "opus"
    with pytest.raises(SystemExit):
        p.parse_args(["--executor", "outro"])


def test_cli_modelo_entra_no_comando(amb: Ambiente) -> None:
    assert "--model" not in loop.montar_comando(amb.config)
    amb.config.modelo = "sonnet"
    cmd = loop.montar_comando(amb.config)
    assert cmd[cmd.index("--model") + 1] == "sonnet"


def test_sdk_sem_chave_encerra_sem_rodar_volta(amb: Ambiente, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    amb.config.executor = "sdk"
    chamadas: list = []
    deps = amb.deps()
    deps.executor_sdk = lambda *a, **k: chamadas.append(a)
    assert loop.executar_loop(amb.config, deps) == "sem_chave_api"
    assert chamadas == [] and amb.chamadas == []
    assert any("ANTHROPIC_API_KEY" in m for m in amb.mensagens)


def test_sdk_com_chave_roda_volta_pelo_executor_sdk(amb: Ambiente, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    amb.config.executor = "sdk"
    amb.config.modelo = "sonnet"
    amb.config.max_voltas = 1
    chamadas: list = []

    def executor_sdk(prompt, **kw):
        chamadas.append((prompt, kw))
        amb.concluir("0.2")
        return loop.ResultadoProcesso(0, ok_json("pelo sdk", 3.0))

    deps = amb.deps()
    deps.executor_sdk = executor_sdk
    assert loop.executar_loop(amb.config, deps) == "max_voltas"
    assert len(chamadas) == 1 and chamadas[0][0] == loop.PROMPT_CONTINUACAO
    assert chamadas[0][1]["modelo"] == "sonnet" and chamadas[0][1]["modo_permissao"] == "auto"
    assert amb.chamadas_claude() == []  # o subprocesso do claude não foi usado
    assert amb.linhas_log()[0]["custo_usd"] == 3.0


def test_simular_mostra_executor(amb: Ambiente, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    amb.config.executor = "sdk"
    assert loop.simular(amb.config, amb.deps()) == 0
    saida = "\n".join(amb.mensagens)
    assert "Executor: sdk" in saida and "--permission-prompts" not in saida


# ------------------------------------------------------------------ modelo, chave e consumo


def _roda_sdk_com_query(amb: Ambiente, monkeypatch, *mensagens, modelo=None):
    """Volta completa no modo sdk com `query` falsa; devolve o que o `query` recebeu."""
    pytest.importorskip("claude_agent_sdk")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "chave-falsa-1234")
    amb.config.executor = "sdk"
    amb.config.modelo = modelo
    amb.config.max_voltas = 1
    query, cap = query_falsa(*mensagens)
    deps = amb.deps()
    deps.executor_sdk = lambda prompt, **kw: loop.executar_via_sdk(prompt, query=query, **kw)
    assert loop.executar_loop(amb.config, deps) == "max_voltas"
    return cap


def test_sdk_modelo_padrao_chega_ao_query(amb: Ambiente, monkeypatch) -> None:
    cap = _roda_sdk_com_query(amb, monkeypatch, ResultMessage())
    assert loop.MODELO_PADRAO_SDK == "claude-sonnet-5-5"
    assert cap["options"].model == "claude-sonnet-5-5"
    assert amb.linhas_log()[0]["modelo"] == "claude-sonnet-5-5"


def test_sdk_modelo_informado_prevalece(amb: Ambiente, monkeypatch) -> None:
    cap = _roda_sdk_com_query(amb, monkeypatch, ResultMessage(), modelo="opus")
    assert cap["options"].model == "opus"


def test_cli_sem_modelo_continua_sem_model(amb: Ambiente) -> None:
    assert loop.modelo_efetivo(amb.config) is None
    assert "--model" not in loop.montar_comando(amb.config)


@pytest.mark.parametrize("flag", ["--modelo", "--model"])
def test_flag_de_modelo_sinonimos(flag: str, amb: Ambiente, monkeypatch) -> None:
    a = loop.criar_parser().parse_args([flag, "opus"])
    assert a.modelo == "opus"
    amb.config.modelo = a.modelo
    cmd = loop.montar_comando(amb.config)
    assert cmd[cmd.index("--model") + 1] == "opus"
    cap = _roda_sdk_com_query(amb, monkeypatch, ResultMessage(), modelo=a.modelo)
    assert cap["options"].model == "opus"


@pytest.mark.parametrize("valor", ["", "   "])
def test_sdk_chave_vazia_ou_so_espacos_encerra(amb: Ambiente, monkeypatch, valor: str) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", valor)
    amb.config.executor = "sdk"
    chamadas: list = []
    deps = amb.deps()
    deps.executor_sdk = lambda *a, **k: chamadas.append(a)
    assert loop.executar_loop(amb.config, deps) == "sem_chave_api"
    assert chamadas == [] and amb.chamadas == []
    assert any("ausente ou vazia" in m and "Use-AnthropicSdkKey" in m for m in amb.mensagens)
    assert amb.linhas_log() == []


def test_simular_chave_vazia_avisa(amb: Ambiente, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", " ")
    amb.config.executor = "sdk"
    assert loop.simular(amb.config, amb.deps()) == 0
    saida = "\n".join(amb.mensagens)
    assert "ausente ou vazia" in saida and "model=claude-sonnet-5-5" in saida


def test_sdk_nao_invoca_claude_e_chave_nao_vaza(amb: Ambiente, monkeypatch, capsys) -> None:
    _roda_sdk_com_query(amb, monkeypatch, ResultMessage())
    assert amb.chamadas_claude() == []
    assert all("custos" in c and c[0] != "claude" for c in amb.chamadas)
    saida = "\n".join(amb.mensagens)
    assert f"ANTHROPIC_API_KEY presente ({len('chave-falsa-1234')} caracteres)." in saida
    assert saida.count("presente (") == 1
    tudo = saida + capsys.readouterr().out
    tudo += amb.config.log.read_text(encoding="utf-8")
    assert "chave-falsa-1234" not in tudo


def test_log_traz_modelo_e_tokens_do_sdk(amb: Ambiente, monkeypatch) -> None:
    msg = ResultMessage(
        usage={
            "input_tokens": 100,
            "output_tokens": 40,
            "cache_read_input_tokens": 7,
            "cache_creation_input_tokens": 3,
        },
        model_usage={"claude-sonnet-5-5": {"inputTokens": 100}},
    )
    _roda_sdk_com_query(amb, monkeypatch, msg, modelo="alias-qualquer")
    linha = amb.linhas_log()[0]
    assert linha["modelo"] == "claude-sonnet-5-5"  # chave única de model_usage vence
    assert linha["tokens_entrada"] == 100 and linha["tokens_saida"] == 40
    assert linha["tokens_cache_leitura"] == 7 and linha["tokens_cache_escrita"] == 3


def test_resultado_para_dict_inclui_model_usage() -> None:
    d = loop.resultado_para_dict(ResultMessage(model_usage={"m": {}}))
    assert d["model_usage"] == {"m": {}} and d["usage"]["input_tokens"] == 10


def test_consumo_cli_model_usage_camel_e_faltantes() -> None:
    saida = {"usage": {"input_tokens": 5}, "modelUsage": {"a": {}, "b": {}}}
    c = loop.extrair_consumo(saida, "cfg")
    assert c == {
        "modelo": "cfg",  # duas chaves: fica o configurado
        "tokens_entrada": 5,
        "tokens_saida": None,
        "tokens_cache_leitura": None,
        "tokens_cache_escrita": None,
    }
    assert loop.extrair_consumo({"modelUsage": {"x": {}}}, None)["modelo"] == "x"
    assert loop.extrair_consumo(None, None)["tokens_entrada"] is None


def test_log_cli_sem_uso_tem_chaves_nulas(amb: Ambiente) -> None:
    amb.respostas = [resp_ok("0.2")]
    amb.config.max_voltas = 1
    amb.rodar()
    linha = amb.linhas_log()[0]
    for k in ("modelo", "tokens_entrada", "tokens_saida", "tokens_cache_leitura"):
        assert linha[k] is None
    assert linha["tokens_cache_escrita"] is None


# ------------------------------------------------------------------ medida de avanço


def _estado_tarefa(**campos: object) -> dict:
    tarefa = {"status": "em_andamento", "descricao": "doc", "observacao": "a", **campos}
    return {"fases": {"3": {"status": "em_andamento", "tarefas": {"3.3": tarefa}}}}


def test_avanco_por_contador_numerico() -> None:
    antes = loop.resumo_estado(_estado_tarefa(lotes_documentados=4))
    depois = loop.resumo_estado(_estado_tarefa(lotes_documentados=8))
    assert loop.houve_avanco(antes, depois)


def test_avanco_por_lista_que_cresce() -> None:
    antes = loop.resumo_estado(_estado_tarefa(lotes_aprovados=["a"]))
    depois = loop.resumo_estado(_estado_tarefa(lotes_aprovados=["a", "b"]))
    assert loop.houve_avanco(antes, depois)


def test_reescrever_observacao_nao_e_avanco() -> None:
    antes = loop.resumo_estado(_estado_tarefa(lotes_documentados=4))
    depois = loop.resumo_estado(_estado_tarefa(lotes_documentados=4, observacao="outra"))
    assert not loop.houve_avanco(antes, depois)


def test_nada_mudou_sem_avanco() -> None:
    estado = _estado_tarefa(lotes_documentados=4, lotes_aprovados=["a"], ok=True)
    assert not loop.houve_avanco(loop.resumo_estado(estado), loop.resumo_estado(estado))


def test_mudanca_de_status_continua_sendo_avanco() -> None:
    antes = loop.resumo_estado(_estado_tarefa())
    depois_estado = _estado_tarefa()
    depois_estado["fases"]["3"]["tarefas"]["3.3"]["status"] = "concluida"
    assert loop.houve_avanco(antes, loop.resumo_estado(depois_estado))
