"""Coletor de custos: marcas de fase, consumo de tokens do Claude Code e relatório.

Veja `docs/CUSTOS.md`. O módulo só lê contagens dos logs do Claude Code: nunca copia
texto de conversa nem caminhos de arquivos de conversa.

Aproximações documentadas:

- Horas de execução: por sessão, a diferença entre a primeira e a última mensagem do
  assistente (as conversas de subagentes entram na sessão que as originou). Pausas
  longas dentro da sessão contam como tempo de execução; é um limite superior.
- Horas por fase/tarefa: a mesma conta, restrita às mensagens atribuídas àquele balde.
- O instante de uma marca tem precisão de segundos; o fim vale até o fim do segundo.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from pg_atlas import __version__
from pg_atlas.cli import adicionar_argumentos_comuns
from pg_atlas.config import Projeto, carregar_projeto
from pg_atlas.erros import ErroConfig
from pg_atlas.json_util import escrever_json

SEM_FASE = "sem_fase"
CAMPOS_TOKENS = ("entrada", "saida", "cache_escrita_5m", "cache_escrita_1h", "cache_leitura")
NOMES_FASES = {
    0: "Fundação",
    1: "Catálogo",
    2: "Relacionamentos",
    3: "Fichas das tabelas",
    4: "Rotinas",
    5: "Receitas e índice",
    6: "Atualização",
    7: "Obsidian",
    8: "Entrega",
}
NAO_INFORMADO = "não informado"
PRECO_NAO_INFORMADO = "preço não informado"


# --------------------------------------------------------------------------- caminhos


def pasta_custos(doc: Path) -> Path:
    return doc / "_controle" / "custos"


def arquivo_fases(doc: Path) -> Path:
    return pasta_custos(doc) / "fases.jsonl"


def arquivo_consumo(doc: Path) -> Path:
    return pasta_custos(doc) / "consumo.json"


def codificar_pasta_workspace(workspace: Path) -> str:
    """Nome da pasta do Claude Code para um workspace: todo caractere que não é
    letra ou dígito vira `-` (`C:\\dev\\atlas` -> `C--dev-atlas`; `.claude` -> `-claude`)."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(workspace))


def pasta_projetos_padrao(workspace: Path) -> Path:
    return Path.home() / ".claude" / "projects" / codificar_pasta_workspace(workspace)


# --------------------------------------------------------------------------- marcas


def agora_local() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def registrar_marca(doc: Path, evento: str, fase: int, tarefa: str | None, quando: str | None = None) -> dict[str, Any]:
    """Acrescenta uma linha ao `fases.jsonl` sem mexer nas linhas anteriores."""
    marca: dict[str, Any] = {"evento": evento, "fase": fase}
    if tarefa:
        marca["tarefa"] = tarefa
    marca["quando"] = quando or agora_local()
    arquivo = arquivo_fases(doc)
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    precisa_quebra = False
    if arquivo.exists() and arquivo.stat().st_size > 0:
        with open(arquivo, "rb") as f:
            f.seek(-1, 2)
            precisa_quebra = f.read(1) != b"\n"
    with open(arquivo, "a", encoding="utf-8", newline="\n") as f:
        if precisa_quebra:
            f.write("\n")
        f.write(json.dumps(marca, ensure_ascii=False) + "\n")
    return marca


@dataclass(frozen=True)
class Intervalo:
    inicio: datetime
    fim: datetime | None  # None: ainda aberto
    fase: int
    tarefa: str | None


def _parse_instante(texto: Any, padrao: timezone | None = None) -> datetime | None:
    if not isinstance(texto, str):
        return None
    try:
        instante = datetime.fromisoformat(texto.strip())
    except ValueError:
        return None
    if instante.tzinfo is None:
        instante = instante.replace(tzinfo=padrao) if padrao else instante.astimezone()
    return instante


def ler_marcas(arquivo: Path) -> list[dict[str, Any]]:
    """Lê o `fases.jsonl`; linhas inválidas são ignoradas. A chave `tarefa` é opcional."""
    marcas: list[dict[str, Any]] = []
    if not arquivo.is_file():
        return marcas
    with open(arquivo, encoding="utf-8") as f:
        for linha in f:
            try:
                obj = json.loads(linha)
            except ValueError:
                continue
            if not isinstance(obj, dict) or obj.get("evento") not in ("inicio", "fim"):
                continue
            fase = obj.get("fase")
            instante = _parse_instante(obj.get("quando"))
            if isinstance(fase, bool) or not isinstance(fase, int) or instante is None:
                continue
            tarefa = obj.get("tarefa")
            marcas.append(
                {
                    "evento": obj["evento"],
                    "fase": fase,
                    "tarefa": str(tarefa) if tarefa not in (None, "") else None,
                    "quando": instante,
                }
            )
    return marcas


def montar_intervalos(marcas: list[dict[str, Any]]) -> list[Intervalo]:
    """Pareia início e fim de cada (fase, tarefa). Início sem fim fica aberto."""
    ordenadas = sorted(enumerate(marcas), key=lambda par: (par[1]["quando"], par[0]))
    abertos: dict[tuple[int, str | None], list[datetime]] = {}
    intervalos: list[Intervalo] = []
    for _, marca in ordenadas:
        chave = (marca["fase"], marca["tarefa"])
        if marca["evento"] == "inicio":
            abertos.setdefault(chave, []).append(marca["quando"])
        elif abertos.get(chave):
            inicio = abertos[chave].pop()
            intervalos.append(Intervalo(inicio, marca["quando"], chave[0], chave[1]))
    for (fase, tarefa), inicios in abertos.items():
        intervalos.extend(Intervalo(i, None, fase, tarefa) for i in inicios)
    return sorted(intervalos, key=lambda i: (i.inicio, i.fase, i.tarefa or ""))


def _contem(intervalo: Intervalo, instante: datetime) -> bool:
    if instante < intervalo.inicio:
        return False
    return intervalo.fim is None or instante < intervalo.fim + timedelta(seconds=1)


def atribuir(intervalos: list[Intervalo], instante: datetime) -> tuple[str, str | None]:
    """Devolve (fase, tarefa). Tarefa aberta mais interna (início mais recente) vence;
    senão a fase aberta mais recente; senão `sem_fase`."""
    for com_tarefa in (True, False):
        candidatos = [
            i for i in intervalos if (i.tarefa is not None) == com_tarefa and _contem(i, instante)
        ]
        if candidatos:
            melhor = max(candidatos, key=lambda i: (i.inicio, i.fase, i.tarefa or ""))
            return str(melhor.fase), melhor.tarefa
    return SEM_FASE, None


# --------------------------------------------------------------------------- logs


@dataclass(frozen=True)
class Resposta:
    chave: str | None
    sessao: str
    modelo: str
    instante: datetime
    tokens: dict[str, int]


def _inteiro(valor: Any) -> int:
    return valor if isinstance(valor, int) and not isinstance(valor, bool) and valor > 0 else 0


def extrair_tokens(usage: dict[str, Any]) -> dict[str, int]:
    total_escrita = _inteiro(usage.get("cache_creation_input_tokens"))
    detalhe = usage.get("cache_creation")
    e5 = e1 = 0
    if isinstance(detalhe, dict):
        e5 = _inteiro(detalhe.get("ephemeral_5m_input_tokens"))
        e1 = _inteiro(detalhe.get("ephemeral_1h_input_tokens"))
    if e5 + e1 < total_escrita:
        e5 += total_escrita - (e5 + e1)
    return {
        "entrada": _inteiro(usage.get("input_tokens")),
        "saida": _inteiro(usage.get("output_tokens")),
        "cache_escrita_5m": e5,
        "cache_escrita_1h": e1,
        "cache_leitura": _inteiro(usage.get("cache_read_input_tokens")),
    }


def interpretar_linha(linha: str, sessao_padrao: str) -> Resposta | None:
    """Extrai só contagens de uma linha do log; devolve None para o que não interessa."""
    try:
        obj = json.loads(linha)
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return None
    mensagem = obj.get("message")
    if not isinstance(mensagem, dict):
        return None
    modelo = mensagem.get("model")
    usage = mensagem.get("usage")
    if not isinstance(modelo, str) or not modelo or modelo.startswith("<") or not isinstance(usage, dict):
        return None
    instante = _parse_instante(obj.get("timestamp"), timezone.utc)
    if instante is None:
        return None
    id_msg = mensagem.get("id")
    id_req = obj.get("requestId")
    if isinstance(id_msg, str) and id_msg:
        chave: str | None = "m:" + id_msg
    elif isinstance(id_req, str) and id_req:
        chave = "r:" + id_req
    else:
        chave = None
    sessao = obj.get("sessionId")
    return Resposta(
        chave=chave,
        sessao=sessao if isinstance(sessao, str) and sessao else sessao_padrao,
        modelo=modelo,
        instante=instante,
        tokens=extrair_tokens(usage),
    )


def listar_logs(pasta: Path) -> list[Path]:
    """Todos os `.jsonl` da pasta, inclusive `<sessao>/subagents/*.jsonl`, em ordem fixa."""
    if not pasta.is_dir():
        return []
    return sorted(pasta.rglob("*.jsonl"), key=lambda p: p.relative_to(pasta).as_posix())


def ler_respostas(arquivos: Iterable[Path]) -> Iterator[Resposta]:
    for arquivo in arquivos:
        try:
            with open(arquivo, encoding="utf-8", errors="replace") as f:
                for linha in f:
                    resposta = interpretar_linha(linha, arquivo.stem)
                    if resposta is not None:
                        yield resposta
        except OSError:
            continue


def deduplicar(respostas: Iterable[Resposta]) -> list[Resposta]:
    """A mesma resposta aparece em várias linhas (uma por bloco): ficam os tokens da
    última ocorrência e o instante da primeira (início da resposta)."""
    por_chave: dict[str, Resposta] = {}
    sem_chave: list[Resposta] = []
    for r in respostas:
        if r.chave is None:
            sem_chave.append(r)
        else:
            anterior = por_chave.get(r.chave)
            por_chave[r.chave] = r if anterior is None else replace(r, instante=min(r.instante, anterior.instante))
    return [*por_chave.values(), *sem_chave]


# --------------------------------------------------------------------------- agregação


def _tokens_vazios() -> dict[str, int]:
    return {campo: 0 for campo in CAMPOS_TOKENS} | {"respostas": 0}


def _somar(destino: dict[str, int], tokens: dict[str, int]) -> None:
    for campo in CAMPOS_TOKENS:
        destino[campo] += tokens[campo]
    destino["respostas"] += 1


def _horas(spans: Iterable[tuple[datetime, datetime]]) -> float:
    return round(sum((fim - ini).total_seconds() for ini, fim in spans) / 3600, 4)


def agregar(respostas: list[Resposta], intervalos: list[Intervalo]) -> dict[str, Any]:
    por_modelo: dict[str, dict[str, int]] = {}
    fases: dict[str, dict[str, Any]] = {}
    span_sessao: dict[str, list[datetime]] = {}
    span_balde: dict[tuple[str, str | None, str], list[datetime]] = {}

    def balde_fase(fase: str) -> dict[str, Any]:
        return fases.setdefault(fase, {"modelos": {}, "tarefas": {}, "horas": 0.0})

    for r in respostas:
        _somar(por_modelo.setdefault(r.modelo, _tokens_vazios()), r.tokens)
        s = span_sessao.setdefault(r.sessao, [r.instante, r.instante])
        s[0], s[1] = min(s[0], r.instante), max(s[1], r.instante)

        fase, tarefa = atribuir(intervalos, r.instante)
        destino = balde_fase(fase)
        _somar(destino["modelos"].setdefault(r.modelo, _tokens_vazios()), r.tokens)
        spans_chaves = [(fase, None, r.sessao)]
        if tarefa is not None:
            t = destino["tarefas"].setdefault(tarefa, {"modelos": {}, "horas": 0.0})
            _somar(t["modelos"].setdefault(r.modelo, _tokens_vazios()), r.tokens)
            spans_chaves.append((fase, tarefa, r.sessao))
        for chave in spans_chaves:
            sp = span_balde.setdefault(chave, [r.instante, r.instante])
            sp[0], sp[1] = min(sp[0], r.instante), max(sp[1], r.instante)

    horas_fase: dict[str, list[tuple[datetime, datetime]]] = {}
    horas_tarefa: dict[tuple[str, str], list[tuple[datetime, datetime]]] = {}
    for (fase, tarefa, _), (ini, fim) in span_balde.items():
        if tarefa is None:
            horas_fase.setdefault(fase, []).append((ini, fim))
        else:
            horas_tarefa.setdefault((fase, tarefa), []).append((ini, fim))
    for fase, spans in horas_fase.items():
        fases[fase]["horas"] = _horas(spans)
    for (fase, tarefa), spans in horas_tarefa.items():
        fases[fase]["tarefas"][tarefa]["horas"] = _horas(spans)

    instantes = [r.instante for r in respostas]
    utc = timezone.utc
    return {
        "esquema": 1,
        "periodo": {
            "inicio": min(instantes).astimezone(utc).strftime("%Y-%m-%dT%H:%M:%SZ") if instantes else None,
            "fim": max(instantes).astimezone(utc).strftime("%Y-%m-%dT%H:%M:%SZ") if instantes else None,
        },
        "sessoes": len(span_sessao),
        "horas_execucao": _horas((ini, fim) for ini, fim in span_sessao.values()),
        "respostas": len(respostas),
        "modelos": por_modelo,
        "fases": fases,
        "observacoes": {
            "horas_execucao": "soma, por sessão, do intervalo entre a primeira e a última mensagem do assistente (limite superior)",
            "deduplicacao": "por message.id (ou requestId), mantida a última ocorrência",
        },
    }


def coletar_consumo(doc: Path, pasta_projetos: Path) -> dict[str, Any]:
    intervalos = montar_intervalos(ler_marcas(arquivo_fases(doc)))
    respostas = deduplicar(ler_respostas(listar_logs(pasta_projetos)))
    return agregar(respostas, intervalos)


# --------------------------------------------------------------------------- preços e câmbio


@dataclass(frozen=True)
class Preco:
    valores: dict[str, float]
    fonte: str
    consultado_em: str

    def completo_para(self, tokens: dict[str, int]) -> bool:
        return all(campo in self.valores or tokens.get(campo, 0) == 0 for campo in CAMPOS_TOKENS)


def _ler_yaml(arquivo: Path) -> dict[str, Any]:
    if not arquivo.is_file():
        return {}
    try:
        dados = yaml.safe_load(arquivo.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        raise ErroConfig(f"YAML inválido em {arquivo}") from None
    if not isinstance(dados, dict):
        raise ErroConfig(f"{arquivo} deve conter um mapa na raiz.")
    return dados


def _numero(valor: Any) -> float | None:
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    return float(valor) if valor >= 0 else None


def carregar_precos(doc: Path) -> dict[str, Preco]:
    modelos = _ler_yaml(doc / "config" / "precos.yml").get("modelos")
    precos: dict[str, Preco] = {}
    if not isinstance(modelos, dict):
        return precos
    for nome, dados in modelos.items():
        if not isinstance(dados, dict):
            continue
        valores = {c: v for c in CAMPOS_TOKENS if (v := _numero(dados.get(c))) is not None}
        precos[str(nome)] = Preco(valores, str(dados.get("fonte") or ""), str(dados.get("consultado_em") or ""))
    return precos


def buscar_preco(precos: dict[str, Preco], modelo: str) -> Preco | None:
    """Nome exato; senão a maior chave que seja prefixo do modelo (sufixos de data)."""
    if modelo in precos:
        return precos[modelo]
    prefixos = [chave for chave in precos if modelo.startswith(chave)]
    return precos[max(prefixos, key=len)] if prefixos else None


@dataclass(frozen=True)
class Cambio:
    ptax_venda: float
    fonte: str
    data: str


def carregar_cambio(doc: Path) -> Cambio | None:
    dados = _ler_yaml(doc / "config" / "cambio.yml")
    taxa = _numero(dados.get("ptax_venda"))
    if taxa is None or taxa <= 0:
        return None
    return Cambio(taxa, str(dados.get("fonte") or ""), str(dados.get("data") or ""))


def custo_usd(modelo: str, tokens: dict[str, int], precos: dict[str, Preco]) -> float | None:
    """Custo equivalente em dólar; None se faltar preço para algum tipo com consumo."""
    preco = buscar_preco(precos, modelo)
    if preco is None or not preco.completo_para(tokens):
        return None
    return sum(tokens.get(c, 0) * preco.valores.get(c, 0.0) for c in CAMPOS_TOKENS) / 1_000_000


# --------------------------------------------------------------------------- produção


def contar_producao(doc: Path) -> dict[str, int]:
    schemas = doc / "schemas"

    def contar(padrao: str) -> int:
        return sum(1 for _ in schemas.glob(padrao)) if schemas.is_dir() else 0

    receitas = doc / "receitas"
    return {
        "tabelas": contar("*/tabelas/*.md"),
        "rotinas": contar("*/funcoes/*.md"),
        "receitas": sum(1 for _ in receitas.rglob("*.md")) if receitas.is_dir() else 0,
    }


# --------------------------------------------------------------------------- relatório


def _fmt_int(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _fmt_dec(x: float, casas: int = 2) -> str:
    return f"{x:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _somar_modelos(baldes: Iterable[dict[str, dict[str, int]]]) -> dict[str, dict[str, int]]:
    total: dict[str, dict[str, int]] = {}
    for modelos in baldes:
        for modelo, tokens in modelos.items():
            destino = total.setdefault(modelo, _tokens_vazios())
            for campo in (*CAMPOS_TOKENS, "respostas"):
                destino[campo] += tokens.get(campo, 0)
    return total


def _soma_tokens(modelos: dict[str, dict[str, int]]) -> dict[str, int]:
    return {c: sum(t.get(c, 0) for t in modelos.values()) for c in CAMPOS_TOKENS}


def _custo_balde(modelos: dict[str, dict[str, int]], precos: dict[str, Preco]) -> float | None:
    """Soma o custo dos modelos; None se algum modelo com consumo estiver sem preço."""
    total = 0.0
    for modelo, tokens in modelos.items():
        valor = custo_usd(modelo, tokens, precos)
        if valor is None:
            return None
        total += valor
    return total


def _fmt_usd(valor: float | None) -> str:
    return PRECO_NAO_INFORMADO if valor is None else f"US$ {_fmt_dec(valor)}"


def _fmt_brl(valor_usd: float | None, cambio: Cambio | None) -> str:
    if valor_usd is None:
        return PRECO_NAO_INFORMADO
    if cambio is None:
        return "câmbio não informado"
    return f"R$ {_fmt_dec(valor_usd * cambio.ptax_venda)}"


def _rotulo_fase(chave: str) -> str:
    if chave == SEM_FASE:
        return "Sem fase"
    try:
        return f"{int(chave)} {NOMES_FASES.get(int(chave), '')}".strip()
    except ValueError:
        return chave


def _ordem_fase(chave: str) -> tuple[int, int]:
    return (1, 0) if chave == SEM_FASE else (0, int(chave))


def _linha_fase(rotulo: str, horas: float, modelos: dict[str, dict[str, int]], precos: dict[str, Preco], cambio: Cambio | None) -> str:
    soma = _soma_tokens(modelos)
    custo = _custo_balde(modelos, precos)
    cache = f"{_fmt_int(soma['cache_escrita_5m'] + soma['cache_escrita_1h'])} / {_fmt_int(soma['cache_leitura'])}"
    return (
        f"| {rotulo} | {_fmt_dec(horas)} | {_fmt_int(soma['entrada'])} | {_fmt_int(soma['saida'])} | {cache} "
        f"| {_fmt_usd(custo)} | {_fmt_brl(custo, cambio)} |"
    )


def _linha_preco(preco: Preco | None) -> str:
    if preco is None:
        return PRECO_NAO_INFORMADO
    partes = [f"{rotulo} {_fmt_dec(preco.valores[c])}" for c, rotulo in (
        ("entrada", "entrada"), ("saida", "saída"), ("cache_escrita_5m", "escrita 5m"),
        ("cache_escrita_1h", "escrita 1h"), ("cache_leitura", "leitura")) if c in preco.valores]
    return "US$/Mtok: " + "; ".join(partes) if partes else PRECO_NAO_INFORMADO


def gerar_relatorio(
    projeto: Projeto,
    consumo: dict[str, Any],
    precos: dict[str, Preco],
    cambio: Cambio | None,
    producao: dict[str, int],
    fase: int | None = None,
    hoje: date | None = None,
) -> str:
    """Monta o Markdown. Com `fase`, é o relatório parcial daquela fase."""
    hoje = hoje or date.today()
    todas_fases: dict[str, Any] = consumo.get("fases", {})
    if fase is None:
        chaves = sorted(todas_fases, key=_ordem_fase)
        modelos = _somar_modelos(f["modelos"] for f in todas_fases.values())
        horas = float(consumo.get("horas_execucao", 0.0))
        sessoes = int(consumo.get("sessoes", 0))
    else:
        chaves = [str(fase)] if str(fase) in todas_fases else []
        modelos = _somar_modelos(todas_fases[k]["modelos"] for k in chaves)
        horas = float(sum(todas_fases[k].get("horas", 0.0) for k in chaves))
        sessoes = int(consumo.get("sessoes", 0))

    periodo = consumo.get("periodo", {})
    inicio = (periodo.get("inicio") or "—")[:10]
    fim = (periodo.get("fim") or "—")[:10]
    custo_total = _custo_balde(modelos, precos)
    avisos: list[str] = []
    sem_preco = sorted(m for m, t in modelos.items() if custo_usd(m, t, precos) is None)
    if sem_preco:
        avisos.append("Preço não informado em `config/precos.yml` para: " + ", ".join(f"`{m}`" for m in sem_preco) + ".")
    if cambio is None:
        avisos.append("Câmbio não informado em `config/cambio.yml`: valores em reais indisponíveis.")

    dados_custos = projeto.dados.get("custos") if isinstance(projeto.dados.get("custos"), dict) else {}
    plano = str(dados_custos.get("plano") or "").strip()
    mensal = _numero(dados_custos.get("valor_mensal_brl"))

    objetos = producao["tabelas"] + producao["rotinas"]
    custo_brl = custo_total * cambio.ptax_venda if custo_total is not None and cambio else None
    if custo_brl is None:
        por_objeto = PRECO_NAO_INFORMADO if custo_total is None or cambio is None else NAO_INFORMADO
    elif objetos == 0:
        por_objeto = "não calculável (nenhum objeto documentado)"
    else:
        por_objeto = f"R$ {_fmt_dec(custo_brl / objetos)}"

    mensal_txt = "não disponível (depende do consumo da fase 6)"
    if fase is None and "6" in todas_fases:
        c6 = _custo_balde(todas_fases["6"]["modelos"], precos)
        if c6 is not None and cambio is not None:
            mensal_txt = f"R$ {_fmt_dec(c6 * cambio.ptax_venda)}"

    desembolso_resumo = NAO_INFORMADO
    desembolso_detalhe = f"Plano: {plano or NAO_INFORMADO}. Valor mensal: {NAO_INFORMADO}. Gasto em API: {NAO_INFORMADO}."
    if plano or mensal is not None:
        valor = f"R$ {_fmt_dec(mensal)}" if mensal is not None else NAO_INFORMADO
        desembolso_detalhe = (
            f"Plano: {plano or NAO_INFORMADO}. Valor mensal: {valor} (fonte: `config/projeto.yml`, "
            f"`custos.plano` e `custos.valor_mensal_brl`). Período: {inicio} a {fim}. "
            f"Gasto em API: {NAO_INFORMADO}."
        )
        if mensal is not None:
            desembolso_resumo = f"{valor} por mês (plano {plano or NAO_INFORMADO})"

    titulo = "Relatório de custo da implementação" if fase is None else f"Relatório parcial de custo, fase {fase}"
    linhas = [
        f"# {titulo}",
        "",
        f"**Projeto:** {projeto.nome or NAO_INFORMADO}",
        f"**Período:** {inicio} a {fim}",
        f"**Gerado em:** {hoje.isoformat()} por pg-atlas {__version__}",
        "",
        "## Resumo",
        "",
        "| Item | Valor |",
        "|---|---|",
        f"| Desembolso real no período | {desembolso_resumo} |",
        f"| Custo equivalente em API | {_fmt_usd(custo_total) if custo_total is None else f'{_fmt_usd(custo_total)} ({_fmt_brl(custo_total, cambio)})'} |",
        f"| Horas de execução | {_fmt_dec(horas)} |",
        f"| Sessões | {sessoes} |",
        f"| Objetos documentados | {producao['tabelas']} tabelas e views, {producao['rotinas']} rotinas |",
        f"| Receitas de consulta validadas | {producao['receitas']} |",
        f"| Custo equivalente por objeto documentado | {por_objeto} |",
        f"| Custo estimado de cada atualização mensal | {mensal_txt} |",
        "",
        "## Custo por fase",
        "",
        "| Fase | Horas | Tokens de entrada | Tokens de saída | Cache (escrita/leitura) | Equivalente US$ | Equivalente R$ |",
        "|---|---|---|---|---|---|---|",
    ]
    if fase is None:
        for numero, nome in NOMES_FASES.items():
            k = str(numero)
            if k in todas_fases:
                linhas.append(_linha_fase(f"{numero} {nome}", todas_fases[k].get("horas", 0.0), todas_fases[k]["modelos"], precos, cambio))
            else:
                linhas.append(f"| {numero} {nome} | — | — | — | — | — | — |")
        extras = [k for k in sorted(todas_fases, key=_ordem_fase) if k != SEM_FASE and int(k) not in NOMES_FASES]
        for k in [*extras, *([SEM_FASE] if SEM_FASE in todas_fases else [])]:
            linhas.append(_linha_fase(_rotulo_fase(k), todas_fases[k].get("horas", 0.0), todas_fases[k]["modelos"], precos, cambio))
        linhas.append(_linha_fase("**Total**", horas, modelos, precos, cambio))
    else:
        for k in chaves:
            linhas.append(_linha_fase(_rotulo_fase(k), todas_fases[k].get("horas", 0.0), todas_fases[k]["modelos"], precos, cambio))
            for tarefa in sorted(todas_fases[k].get("tarefas", {})):
                t = todas_fases[k]["tarefas"][tarefa]
                linhas.append(_linha_fase(f"&nbsp;&nbsp;tarefa {tarefa}", t.get("horas", 0.0), t["modelos"], precos, cambio))
        if not chaves:
            linhas.append(f"| {_rotulo_fase(str(fase))} | — | — | — | — | — | — |")

    linhas += [
        "",
        "## Consumo por modelo",
        "",
        "| Modelo | Tokens de entrada | Tokens de saída | Cache | Preço usado (por milhão) | Equivalente US$ |",
        "|---|---|---|---|---|---|",
    ]
    for modelo in sorted(modelos):
        t = modelos[modelo]
        cache = f"escrita 5m {_fmt_int(t['cache_escrita_5m'])}, escrita 1h {_fmt_int(t['cache_escrita_1h'])}, leitura {_fmt_int(t['cache_leitura'])}"
        preco = buscar_preco(precos, modelo)
        linhas.append(
            f"| {modelo} | {_fmt_int(t['entrada'])} | {_fmt_int(t['saida'])} | {cache} "
            f"| {_linha_preco(preco)} | {_fmt_usd(custo_usd(modelo, t, precos))} |"
        )
    if not modelos:
        linhas.append("| — | — | — | — | — | — |")

    fontes_precos = sorted({f"{p.fonte} (consultado em {p.consultado_em or 'data não informada'})" for p in precos.values() if p.fonte})
    linhas += [
        "",
        "## Desembolso real",
        "",
        desembolso_detalhe,
        "",
        "## Premissas",
        "",
        "- Preços: " + ("; ".join(fontes_precos) if fontes_precos else PRECO_NAO_INFORMADO) + ".",
        (
            f"- Câmbio: PTAX de venda de {cambio.data or 'data não informada'}, R$ {_fmt_dec(cambio.ptax_venda, 4)} por dólar, fonte {cambio.fonte or 'não informada'}."
            if cambio
            else "- Câmbio: não informado."
        ),
        "- O custo equivalente em API é uma estimativa do valor do consumo, não o valor pago.",
        "- Não inclui horas de trabalho humano, energia nem infraestrutura da prefeitura.",
        "- Horas de execução: soma, por sessão, do intervalo entre a primeira e a última mensagem do assistente (aproximação; inclui pausas dentro da sessão).",
        "- Objetos documentados e receitas: contagem dos arquivos em `schemas/*/tabelas`, `schemas/*/funcoes` e `receitas/`; receitas contadas ainda não distinguem as validadas.",
    ]
    for aviso in avisos:
        linhas.append(f"- AVISO: {aviso}")

    sem_fase = todas_fases.get(SEM_FASE)
    limitacoes = ["", "## Limitações", ""]
    if sem_fase:
        n = sum(t.get("respostas", 0) for t in sem_fase["modelos"].values())
        limitacoes.append(f"- {n} respostas ocorreram fora de qualquer marca de fase (`sem_fase`); no relatório completo entram no total.")
    if not consumo.get("sessoes"):
        limitacoes.append("- Nenhuma sessão encontrada: confira `custos coletar`.")
    limitacoes.append("- Sessões fora da pasta do workspace e períodos sem log não são medidos; o formato dos logs pode mudar entre versões do Claude Code.")
    linhas += limitacoes
    return "\n".join(linhas) + "\n"


# --------------------------------------------------------------------------- comandos


def _cmd_marcar(evento: str):  # type: ignore[no-untyped-def]
    def executar(args: argparse.Namespace) -> int:
        doc = Path(args.doc).resolve()
        if not doc.is_dir():
            raise ErroConfig(f"Pasta de documentação não encontrada: {doc}")
        marca = registrar_marca(doc, evento, args.fase, args.tarefa)
        print(f"Marca registrada: {json.dumps(marca, ensure_ascii=False)}")
        return 0

    return executar


def _cmd_coletar(args: argparse.Namespace) -> int:
    projeto = carregar_projeto(args.doc, args.workspace)
    pasta = Path(args.projetos) if args.projetos else pasta_projetos_padrao(projeto.workspace)
    consumo = coletar_consumo(projeto.doc, pasta)
    escrever_json(arquivo_consumo(projeto.doc), consumo)
    print(
        f"Consumo gravado em {arquivo_consumo(projeto.doc)}: {consumo['sessoes']} sessões, "
        f"{consumo['respostas']} respostas, {consumo['horas_execucao']:.2f} h."
    )
    return 0


def _cmd_relatorio(args: argparse.Namespace) -> int:
    projeto = carregar_projeto(args.doc, args.workspace)
    if args.parcial and args.fase is None:
        raise ErroConfig("--parcial exige --fase N.")
    arquivo = arquivo_consumo(projeto.doc)
    if not arquivo.is_file():
        raise ErroConfig(f"Consumo não coletado: rode `custos coletar` antes ({arquivo}).")
    consumo = json.loads(arquivo.read_text(encoding="utf-8"))
    texto = gerar_relatorio(
        projeto,
        consumo,
        carregar_precos(projeto.doc),
        carregar_cambio(projeto.doc),
        contar_producao(projeto.doc),
        fase=args.fase if args.parcial else None,
    )
    nome = f"custo-parcial-fase-{args.fase}.md" if args.parcial else "custo-implementacao.md"
    destino = projeto.doc / "relatorios" / nome
    destino.parent.mkdir(parents=True, exist_ok=True)
    with open(destino, "w", encoding="utf-8", newline="\n") as f:
        f.write(texto)
    print(f"Relatório gravado em {destino}")
    return 0


def registrar(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("custos", help="marcas de fase, coleta de consumo e relatório de custo")
    sub = parser.add_subparsers(dest="acao", metavar="ação", required=True)

    for evento, ajuda in (("inicio", "registra o início de uma fase/tarefa"), ("fim", "registra o fim de uma fase/tarefa")):
        p = sub.add_parser(f"marcar-{evento}", help=ajuda)
        adicionar_argumentos_comuns(p)
        p.add_argument("--fase", type=int, required=True)
        p.add_argument("--tarefa", default=None, help="ex.: 0.8 (opcional)")
        p.set_defaults(func=_cmd_marcar(evento))

    p = sub.add_parser("coletar", help="soma os tokens dos logs do Claude Code por modelo, fase e tarefa")
    adicionar_argumentos_comuns(p)
    p.add_argument("--projetos", default=None, help="pasta dos .jsonl (padrão: ~/.claude/projects/<workspace codificado>)")
    p.set_defaults(func=_cmd_coletar)

    p = sub.add_parser("relatorio", help="gera o relatório de custo em Markdown")
    adicionar_argumentos_comuns(p)
    p.add_argument("--parcial", action="store_true", help="relatório de uma fase (exige --fase)")
    p.add_argument("--fase", type=int, default=None)
    p.set_defaults(func=_cmd_relatorio)

