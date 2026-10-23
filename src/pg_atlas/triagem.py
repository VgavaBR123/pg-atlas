"""Comando `triagem`: classifica cada tabela, sem LLM e sem conexão (decisão D-012).

Lê só `<doc>/catalogo/` (`_banco.json`, `estrutura/*.json`, `estatisticas/*.json`) e grava:
- `catalogo/_triagem.json` (determinístico, validado por `esquemas/triagem.schema.json`);
- `config/dominios-candidatos.yml` (sobrescrito a cada execução; `dominios.yml` nunca é tocado);
- o item `fases["1"]["tarefas"]["1.3"]` de `_controle/estado.json`.

Classes, na ordem de precedência: `particao`, `vazia`, `temporaria_ou_copia`,
`dominio_candidata`, `fato_grande`, `comum`. Os sinais que casaram ficam todos gravados.
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from datetime import date
from importlib import resources
from pathlib import Path
from typing import Any

import jsonschema

from pg_atlas.cli import adicionar_argumentos_comuns
from pg_atlas.config import carregar_projeto
from pg_atlas.erros import ErroAtlas, ErroConfig
from pg_atlas.json_util import escrever_json

VERSAO_FORMATO = 1
PASTA = "catalogo"
FASE = "1"
TAREFA = "1.3"
SAIDA = "_triagem.json"
CANDIDATAS = "dominios-candidatos.yml"

LIMITE_DOMINIO_LINHAS = 500
LIMITE_FATO_LINHAS = 1_000_000
CLASSES = ("particao", "vazia", "temporaria_ou_copia", "dominio_candidata", "fato_grande", "comum")
TIPOS_TRIADOS = ("tabela", "particionada")
# Termo de domínio (singular, como no plano) -> formas aceitas no nome (singular e plural).
TERMOS_DOMINIO: dict[str, tuple[str, ...]] = {
    "categoria": ("categoria", "categorias"),
    "classe": ("classe", "classes"),
    "configuracao": ("configuracao", "configuracoes"),
    "motivo": ("motivo", "motivos"),
    "natureza": ("natureza", "naturezas"),
    "situacao": ("situacao", "situacoes"),
    "status": ("status",),
    "tipo": ("tipo", "tipos"),
}
TERMOS_TEMPORARIA = ("backup", "bkp", "copia", "old", "temp", "tmp")
_FORMA_PARA_TERMO = {forma: termo for termo, formas in TERMOS_DOMINIO.items() for forma in formas}
_MARCADOR = "[mascarado]"
_RE_MARCADOR = re.compile(r"\[mascarado:[a-z]+(?::\d+)?\]")
_RE_SUFIXO_MASCARADO = re.compile(r"\[mascarado:[a-z]+(?::\d+)?\]$")
_RE_SUFIXO_NUMERICO = re.compile(r"_\d+$")


class ErroTriagem(ErroAtlas):
    """Catálogo ausente ou incompleto, ou saída fora do formato."""

    codigo_saida = 4


# ----------------------------------------------------------------- nome


def normalizar(nome: str) -> str:
    """Minúsculas e sem acento (NFKD)."""
    decomposto = unicodedata.normalize("NFKD", nome.lower())
    return "".join(c for c in decomposto if not unicodedata.combining(c))


def tokens_do_nome(nome: str) -> list[str]:
    """Tokens por `_` e por qualquer caractere que não seja letra nem dígito.

    Um marcador `[mascarado:<tipo>:<N>]` vale um token único (`[mascarado]`).
    """
    saida: list[str] = []
    pos = 0
    for m in _RE_MARCADOR.finditer(nome):
        saida += _separar(nome[pos : m.start()])
        saida.append(_MARCADOR)
        pos = m.end()
    return saida + _separar(nome[pos:])


def _separar(texto: str) -> list[str]:
    return [p for p in re.split(r"[^a-z0-9]+", normalizar(texto)) if p]


def _data_valida(ano: int, mes: int, dia: int = 1) -> bool:
    if not 1900 <= ano <= 2099:
        return False
    try:
        date(ano, mes, dia)
    except ValueError:
        return False
    return True


def _hora_valida(hora: int, minuto: int, segundo: int = 0) -> bool:
    return hora < 24 and minuto < 60 and segundo < 60


def _numeros(texto: str, *tamanhos: int) -> list[int]:
    saida, i = [], 0
    for t in tamanhos:
        saida.append(int(texto[i : i + t]))
        i += t
    return saida


def _data_compacta(d: str) -> bool:
    """Dígitos colados: AAAA, AAAAMM/MMAAAA, AAAAMMDD/DDMMAAAA, AAAAMMDDHHMM[SS]."""
    tamanho = len(d)
    if tamanho == 4:
        return 1900 <= int(d) <= 2099
    if tamanho == 6:
        a, m = _numeros(d, 4, 2)
        m2, a2 = _numeros(d, 2, 4)
        return _data_valida(a, m) or _data_valida(a2, m2)
    if tamanho == 8:
        a, m, dia = _numeros(d, 4, 2, 2)
        dia2, m2, a2 = _numeros(d, 2, 2, 4)
        return _data_valida(a, m, dia) or _data_valida(a2, m2, dia2)
    if tamanho in (12, 14):
        a, m, dia, *hms = _numeros(d, 4, 2, 2, 2, 2, *([2] if tamanho == 14 else []))
        return _data_valida(a, m, dia) and _hora_valida(*hms)
    return False


def tem_sufixo_data(nome: str) -> bool:
    """Sufixo de data após `_` no fim do nome (formatos da D-012, item 5)."""
    n = normalizar(nome)
    m = re.search(r"_(\d{4})_(\d{2})_(\d{2})$", n)
    if m and _data_valida(int(m[1]), int(m[2]), int(m[3])):
        return True
    m = re.search(r"_(\d{2})_(\d{2})_(\d{4})$", n)
    if m and _data_valida(int(m[3]), int(m[2]), int(m[1])):
        return True
    m = re.search(r"_(\d{4})_(\d{2})$", n)
    if m and _data_valida(int(m[1]), int(m[2])):
        return True
    m = re.search(r"_(\d+)$", n)
    return bool(m and _data_compacta(m[1]))


def _termo_temporario(tokens: list[str]) -> bool:
    """Primeiro ou último token (ignorando antes os finais numéricos ou mascarados)."""
    fim = list(tokens)
    while fim and (fim[-1].isdigit() or fim[-1] == _MARCADOR):
        fim.pop()
    candidatos = {tokens[0]} if tokens else set()
    if fim:
        candidatos.add(fim[-1])
    return bool(candidatos & set(TERMOS_TEMPORARIA))


def termos_de_dominio(nome: str) -> list[str]:
    """Termos de domínio (singular) que casam como token inteiro, em qualquer posição."""
    return sorted({_FORMA_PARA_TERMO[t] for t in tokens_do_nome(nome) if t in _FORMA_PARA_TERMO})


def sinais_de_nome(nome: str) -> set[str]:
    sinais: set[str] = set()
    if nome.startswith("_"):
        sinais.add("prefixo_underscore")
    if _termo_temporario(tokens_do_nome(nome)):
        sinais.add("termo_temporario")
    if _RE_SUFIXO_MASCARADO.search(nome):
        sinais.add("sufixo_mascarado")
    elif tem_sufixo_data(nome):
        sinais.add("sufixo_data")
    elif _RE_SUFIXO_NUMERICO.search(nome):
        sinais.add("sufixo_numerico")
    if termos_de_dominio(nome):
        sinais.add("termo_dominio")
    return sinais


# ----------------------------------------------------------------- contagens

Chave = tuple[str, str]
Contagens = tuple[int | None, int | None]  # (linhas_estimadas, linhas_vivas)
_SINAIS_TEMPORARIA = ("prefixo_underscore", "sufixo_data", "sufixo_mascarado", "termo_temporario")


def _contagem(valor: Any) -> int | None:
    """Ausente, não numérico ou negativo (reltuples -1) vira desconhecido, nunca 0."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float)) or valor < 0:
        return None
    return int(round(valor))


def _somar(valores: list[int | None]) -> int | None:
    conhecidos = [v for v in valores if v is not None]
    return sum(conhecidos) if conhecidos else None


def somar_particoes(
    chave: Chave,
    proprias: dict[Chave, Contagens],
    filhas: dict[Chave, list[Chave]],
    _visitadas: frozenset[Chave] = frozenset(),
) -> Contagens:
    """Contagens da relação; havendo partições, a soma recursiva delas (todos os níveis)."""
    sub = [f for f in filhas.get(chave, []) if f not in _visitadas]
    if not sub:
        return proprias.get(chave, (None, None))
    partes = [somar_particoes(f, proprias, filhas, _visitadas | {chave}) for f in sub]
    return _somar([p[0] for p in partes]), _somar([p[1] for p in partes])


def classificar(
    sinais: set[str], linhas_triagem: int | None, estimadas: int | None, vivas: int | None,
    eh_particao: bool,
) -> str:
    """Primeira regra que casa, na ordem de precedência da D-012."""
    if eh_particao:
        return "particao"
    if estimadas == 0 and vivas == 0:
        return "vazia"
    if sinais & set(_SINAIS_TEMPORARIA):
        return "temporaria_ou_copia"
    if linhas_triagem is not None:
        if linhas_triagem <= LIMITE_DOMINIO_LINHAS and "termo_dominio" in sinais:
            return "dominio_candidata"
        if linhas_triagem > LIMITE_FATO_LINHAS:
            return "fato_grande"
    return "comum"


def montar_item(
    schema: str, rel: dict[str, Any], contagens: Contagens, tem_filhas: bool
) -> dict[str, Any]:
    nome = rel["nome"]
    estimadas, vivas = contagens
    disponiveis = [v for v in (estimadas, vivas) if v is not None]
    linhas_triagem = max(disponiveis) if disponiveis else None
    particao = rel.get("particao")
    sinais = sinais_de_nome(nome)
    if estimadas is None or vivas is None:
        sinais.add("sem_estatistica")
    if estimadas == 0 and vivas == 0:
        sinais.add("contagem_zero")
    if linhas_triagem is not None:
        if linhas_triagem <= LIMITE_DOMINIO_LINHAS:
            sinais.add("porte_dominio")
        if linhas_triagem > LIMITE_FATO_LINHAS:
            sinais.add("porte_fato")
    if tem_filhas:
        sinais.add("soma_particoes")
    if particao:
        sinais.add("particao")
    return {
        "schema": schema,
        "nome": nome,
        "tipo": rel["tipo"],
        "classe": classificar(sinais, linhas_triagem, estimadas, vivas, bool(particao)),
        "sinais": sorted(sinais),
        "termos_dominio": termos_de_dominio(nome),
        "linhas_estimadas": estimadas,
        "linhas_vivas": vivas,
        "linhas_triagem": linhas_triagem,
        "particao_de": f"{particao.get('pai_schema') or schema}.{particao['pai_nome']}" if particao else None,
        "acesso": rel.get("acesso") or "ok",
    }


# ----------------------------------------------------------------- entrada


def _ler(caminho: Path) -> dict[str, Any]:
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ErroTriagem(f"Não foi possível ler {caminho}. Rode 'pg_atlas catalogo' antes.") from None
    if not isinstance(dados, dict):
        raise ErroTriagem(f"{caminho} deveria conter um objeto JSON. Rode 'pg_atlas catalogo'.")
    return dados


def carregar_catalogo(raiz: Path) -> tuple[str, dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Devolve (extraido_em, estruturas por schema, estatísticas por schema); exige tudo."""
    banco_arq = raiz / "_banco.json"
    if not banco_arq.is_file():
        raise ErroTriagem(f"Catálogo ausente: {banco_arq} não existe. Rode 'pg_atlas catalogo'.")
    banco = _ler(banco_arq)
    schemas = sorted(banco.get("schemas") or {})
    if not schemas:
        raise ErroTriagem("Catálogo vazio (_banco.json sem schemas). Rode 'pg_atlas catalogo'.")
    estruturas, estatisticas = {}, {}
    faltando = []
    for s in schemas:
        for pasta, destino in (("estrutura", estruturas), ("estatisticas", estatisticas)):
            arquivo = raiz / pasta / f"{s}.json"
            if arquivo.is_file():
                destino[s] = _ler(arquivo)
            else:
                faltando.append(f"{pasta}/{arquivo.name}")
    if faltando:
        amostra = ", ".join(faltando[:5]) + (" ..." if len(faltando) > 5 else "")
        raise ErroTriagem(
            f"Catálogo incompleto: {len(faltando)} arquivo(s) ausente(s) ({amostra}). "
            "Rode 'pg_atlas catalogo' até concluir todos os schemas."
        )
    extraido_em = banco.get("extraido_em")
    if not isinstance(extraido_em, str):
        raise ErroTriagem("_banco.json sem 'extraido_em'. Rode 'pg_atlas catalogo'.")
    return extraido_em, estruturas, estatisticas


# ----------------------------------------------------------------- triagem


def triar(
    extraido_em: str, estruturas: dict[str, dict[str, Any]], estatisticas: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Monta o documento `_triagem.json` a partir do catálogo já carregado."""
    proprias: dict[Chave, Contagens] = {}
    for schema, doc in estatisticas.items():
        for r in doc.get("relacoes", []):
            proprias[(schema, r["nome"])] = (
                _contagem(r.get("linhas_estimadas")),
                _contagem(r.get("linhas_vivas")),
            )
    relacoes = {
        (schema, r["nome"]): r
        for schema, doc in estruturas.items()
        for r in doc.get("relacoes", [])
        if r.get("tipo") in TIPOS_TRIADOS
    }
    filhas: dict[Chave, list[Chave]] = {}
    for chave, r in sorted(relacoes.items()):
        p = r.get("particao")
        if p:
            filhas.setdefault((p["pai_schema"], p["pai_nome"]), []).append(chave)
    itens = [
        montar_item(
            schema, r, somar_particoes((schema, nome), proprias, filhas), (schema, nome) in filhas
        )
        for (schema, nome), r in sorted(relacoes.items())
    ]
    por_classe = {c: 0 for c in sorted(CLASSES)}
    por_schema: dict[str, dict[str, int]] = {}
    for i in itens:
        por_classe[i["classe"]] += 1
        por_schema.setdefault(i["schema"], {c: 0 for c in sorted(CLASSES)})[i["classe"]] += 1
    return {
        "formato": "triagem",
        "versao_formato": VERSAO_FORMATO,
        "estatisticas_extraidas_em": extraido_em,
        "criterios": {
            "limite_dominio_linhas": LIMITE_DOMINIO_LINHAS,
            "limite_fato_linhas": LIMITE_FATO_LINHAS,
            "precedencia": list(CLASSES),
            "termos_dominio": sorted(TERMOS_DOMINIO),
            "termos_temporaria": sorted(TERMOS_TEMPORARIA),
        },
        "resumo": {
            "por_classe": por_classe,
            "por_schema": dict(sorted(por_schema.items())),
            "total": len(itens),
        },
        "tabelas": itens,
    }


def carregar_esquema() -> dict[str, Any]:
    texto = (resources.files("pg_atlas.esquemas") / "triagem.schema.json").read_text(encoding="utf-8")
    return json.loads(texto)


def validar(documento: dict[str, Any]) -> None:
    erros = sorted(
        jsonschema.Draft202012Validator(carregar_esquema()).iter_errors(documento),
        key=lambda e: [str(p) for p in e.path],
    )
    if erros:
        onde = "/".join(str(p) for p in erros[0].absolute_path) or "(raiz)"
        raise ErroTriagem(
            f"_triagem.json fora do formato em {onde}: {erros[0].message[:200]} "
            f"({len(erros)} problema(s) no total)."
        )


# ----------------------------------------------------------------- candidatas


def _texto_yaml(valor: str) -> str:
    """Escalar YAML entre aspas duplas (JSON é YAML válido); preserva acentos."""
    return json.dumps(valor, ensure_ascii=False)


def gerar_candidatas(triagem: dict[str, Any]) -> str:
    """Texto de `config/dominios-candidatos.yml`: candidatas ordenadas por `tabela`."""
    candidatas = sorted(
        (
            {
                "tabela": f"{t['schema']}.{t['nome']}",
                "linhas": t["linhas_triagem"],
                "termos": t["termos_dominio"],
                "acesso": t["acesso"],
            }
            for t in triagem["tabelas"]
            if t["classe"] == "dominio_candidata"
        ),
        key=lambda c: c["tabela"],
    )
    linhas = [
        "# Gerado por `pg_atlas triagem`. Não editar: o arquivo é sobrescrito a cada execução.",
        "# Copie para config/dominios.yml (lista `aprovadas`) as tabelas que aprovar,",
        "# no formato `- schema.tabela`.",
        "# Tabelas com acesso `sem_select` aparecem marcadas: sem SELECT a aprovação não tem efeito.",
        "",
    ]
    if not candidatas:
        return "\n".join(linhas) + "\ncandidatas: []\n"
    linhas.append("candidatas:")
    for c in candidatas:
        linhas += [
            f"  - tabela: {_texto_yaml(c['tabela'])}",
            f"    linhas_estimadas: {c['linhas']}",
            f"    termos: [{', '.join(c['termos'])}]",
            f"    acesso: {c['acesso']}",
        ]
    return "\n".join(linhas) + "\n"


def escrever_texto(caminho: Path, texto: str) -> None:
    """Grava de forma atômica em UTF-8 com `\\n`."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = caminho.with_name(caminho.name + ".tmp")
    with open(temporario, "w", encoding="utf-8", newline="\n") as f:
        f.write(texto)
    temporario.replace(caminho)


# ----------------------------------------------------------------- estado.json


def gravar_estado(caminho: Path, resumo: dict[str, Any]) -> None:
    """Atualiza só `fases["1"]["tarefas"]["1.3"]`, preservando o resto do arquivo."""
    estado: dict[str, Any] = {}
    if caminho.is_file():
        try:
            lido = json.loads(caminho.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raise ErroConfig(f"JSON inválido em {caminho}") from None
        estado = lido if isinstance(lido, dict) else {}
    bloco = estado.setdefault("fases", {}).setdefault(FASE, {}).setdefault("tarefas", {}).setdefault(
        TAREFA, {}
    )
    bloco["status"] = "concluida"
    bloco["contagem_por_classe"] = dict(resumo["por_classe"])
    bloco["total_tabelas"] = resumo["total"]
    escrever_json(caminho, estado)


# ----------------------------------------------------------------- execução


def executar_triagem(doc: Path, controle: Path) -> dict[str, Any]:
    raiz = doc / PASTA
    extraido_em, estruturas, estatisticas = carregar_catalogo(raiz)
    triagem = triar(extraido_em, estruturas, estatisticas)
    validar(triagem)
    escrever_json(raiz / SAIDA, triagem)
    escrever_texto(doc / "config" / CANDIDATAS, gerar_candidatas(triagem))
    gravar_estado(controle / "estado.json", triagem["resumo"])
    return triagem


def registrar(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "triagem", help="classifica as tabelas a partir do catálogo (sem conectar ao banco)"
    )
    adicionar_argumentos_comuns(parser)
    parser.set_defaults(func=executar)


def executar(args: argparse.Namespace) -> int:
    projeto = carregar_projeto(args.doc, args.workspace)
    triagem = executar_triagem(projeto.doc, projeto.controle)
    resumo = triagem["resumo"]
    partes = ", ".join(f"{c}={n}" for c, n in resumo["por_classe"].items())
    print(f"Triagem: {resumo['total']} tabelas ({partes}).")
    print(f"Candidatas a domínio: {resumo['por_classe']['dominio_candidata']} (config/{CANDIDATAS}).")
    return 0
