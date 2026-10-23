"""Mascaramento de dados pessoais em texto do catálogo (decisões D-006 e D-009).

Padrões: CPF e CNPJ (só com dígitos verificadores válidos, colados ou não a letras e `_`),
e-mail e telefone brasileiro formatado. E-mail e telefone viram marcador fixo
(`[mascarado:email]`); CPF e CNPJ viram marcador numerado (`[mascarado:cpf:N]`): N é único
por valor distinto no banco todo, atribuído por ordem de primeira aparição, e o mapa
valor -> N fica só em arquivo local fora do git. Tudo é determinístico.
Nenhuma função daqui devolve ou registra o valor encontrado: só tipo, contagem e local.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from typing import Any

from pg_atlas.erros import ErroAtlas


class ErroMascara(ErroAtlas):
    """Colisão de nomes após mascarar ou mapa de máscara inválido."""

    codigo_saida = 4


TIPOS = ("cnpj", "cpf", "email", "credencial", "ip", "telefone")  # ordem de aplicação
NUMERADOS = ("cnpj", "cpf")
NOME_MAPA = "mascaramento-mapa.local.json"

_RE_EMAIL = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
)
_RE_CNPJ = re.compile(r"(?<!\d)\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}(?!\d)")
_RE_CPF = re.compile(r"(?<!\d)\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?!\d)")
_RE_TELEFONE = re.compile(
    r"(?<![\w+])(?:"
    r"\+55[\s.-]?\(?\d{2}\)?[\s.-]?9?\d{4}-?\d{4}"  # com código do país
    r"|\(\d{2}\)[\s.-]?9?\d{4}-?\d{4}"  # (DD) 9XXXX-XXXX
    r"|\d{2}[\s.-]9?\d{4}-\d{4}"  # DD 9XXXX-XXXX (hífen obrigatório)
    r")(?!\w)"
)
_RE_MARCADOR = re.compile(r"\[mascarado:(cnpj|cpf|email|credencial|ip|telefone)(?::(\d+))?\]")


def marcador(tipo: str, numero: int | None = None) -> str:
    return f"[mascarado:{tipo}]" if numero is None else f"[mascarado:{tipo}:{numero}]"


def sem_numero(texto: str) -> str:
    """Tira o N dos marcadores (para registros de auditoria)."""
    return _RE_MARCADOR.sub(lambda m: marcador(m.group(1)), texto)


def nome_de_arquivo(nome: str) -> str:
    """`[mascarado:<tipo>:<N>]` -> `mascarado-<tipo>-<N>` (':' é inválido no Windows)."""
    return _RE_MARCADOR.sub(
        lambda m: "mascarado-" + m.group(1) + (f"-{m.group(2)}" if m.group(2) else ""), nome
    )


# ----------------------------------------------------------------- validações


def _digitos(texto: str) -> list[int]:
    return [int(c) for c in texto if c.isdigit()]


def _dv(digitos: list[int], pesos: list[int]) -> int:
    resto = sum(d * p for d, p in zip(digitos, pesos)) % 11
    return 0 if resto < 2 else 11 - resto


def cpf_valido(texto: str) -> bool:
    d = _digitos(texto)
    if len(d) != 11 or len(set(d)) == 1:
        return False
    return d[9] == _dv(d[:9], list(range(10, 1, -1))) and d[10] == _dv(d[:10], list(range(11, 1, -1)))


def cnpj_valido(texto: str) -> bool:
    d = _digitos(texto)
    if len(d) != 14 or len(set(d)) == 1:
        return False
    p1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    p2 = [6] + p1
    return d[12] == _dv(d[:12], p1) and d[13] == _dv(d[:13], p2)


_VALIDADORES: dict[str, Callable[[str], bool]] = {
    "cnpj": cnpj_valido,
    "cpf": cpf_valido,
    "email": lambda _: True,
    "telefone": lambda _: True,
}
_RE_OCTETO = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_RE_IPV4 = re.compile(rf"(?<![\w.]){_RE_OCTETO}(?:\.{_RE_OCTETO}){{3}}(?![\w]|\.\d)")
_REGEX = {"cnpj": _RE_CNPJ, "cpf": _RE_CPF, "email": _RE_EMAIL, "telefone": _RE_TELEFONE,
          "ip": _RE_IPV4}  # fmt: skip
_VALIDADORES["ip"] = lambda _: True

# Credenciais (D-010): strings de conexão libpq (chave=valor) e URIs com senha.
_CHAVES_CONEXAO = ("host", "hostaddr", "port", "dbname", "user", "password", "passfile",
                   "service", "sslkey", "sslpassword")  # fmt: skip
_CHAVES_SENHA = ("password", "sslpassword")
_NAO_MARCADO = r"(?!\[mascarado:)"
_VALOR_ENTRE_ASPAS_DUPLAS = r"\"[^\"\n]+\""
_VALOR_ESCAPADO = r"''[^'\n]+''"
_VALOR_ENTRE_ASPAS = r"'(?!\s*\|\|)[^'\n]*'"
_VALOR_SEM_ASPAS = r"[^\s'\";,)(\]|$:@]+"
_RE_PAR_CONEXAO = re.compile(
    rf"(?<![\w.])(?P<chave>{'|'.join(_CHAVES_CONEXAO)})(?P<igual>\s*=\s*)"
    rf"(?P<valor>{_NAO_MARCADO}(?:{_VALOR_ESCAPADO}|{_VALOR_ENTRE_ASPAS_DUPLAS}"
    rf"|{_VALOR_ENTRE_ASPAS}|{_VALOR_SEM_ASPAS}))",
    re.IGNORECASE,
)
# Forma OPTIONS (CREATE SERVER / USER MAPPING): `OPTIONS (password 'x', user 'y')`.
_RE_OPTIONS = re.compile(r"\bOPTIONS\s*\((?P<corpo>[^)]*)\)", re.IGNORECASE)
_RE_OPCAO = re.compile(
    rf"(?<![\w.])(?P<chave>{'|'.join(_CHAVES_CONEXAO)})\s+(?P<q>''|')"
    r"(?P<valor>(?!\[mascarado:)[^'\n]+)(?P=q)",
    re.IGNORECASE,
)
_RE_URI = re.compile(r"postgres(?:ql)?://" + _NAO_MARCADO + r"[^\s'\"<>)]+")
_RE_SEPARADOR = re.compile(r"^[\s']{0,4}(?:\|\|[\s']{0,4})?$")


def _mascarar_credenciais(texto: str) -> tuple[str, int]:
    """Redige valores de strings de conexão e URIs; devolve (texto, ocorrências).

    Pares chave=valor só contam em grupo de pelo menos duas chaves seguidas (separadas por
    espaço ou `||`), ou quando a chave é uma senha. Concatenação dinâmica não tem valor literal
    e fica intacta.
    """
    texto, n_uri = _RE_URI.subn("postgresql://" + marcador("credencial"), texto)
    pares = list(_RE_PAR_CONEXAO.finditer(texto))
    grupos: list[list[re.Match[str]]] = []
    for par in pares:
        if grupos and _RE_SEPARADOR.match(texto[grupos[-1][-1].end() : par.start()]):
            grupos[-1].append(par)
        else:
            grupos.append([par])
    escolhidos = []
    for grupo in grupos:
        if len({p["chave"].lower() for p in grupo}) >= 2:
            escolhidos += grupo
        else:
            escolhidos += [p for p in grupo if p["chave"].lower() in _CHAVES_SENHA]
    saida, cursor = [], 0
    for par in escolhidos:
        saida.append(texto[cursor : par.start("valor")])
        saida.append(marcador("credencial"))
        cursor = par.end("valor")
    saida.append(texto[cursor:])
    texto, n_opcoes = _mascarar_options("".join(saida))
    return texto, n_uri + len(escolhidos) + n_opcoes


def _mascarar_options(texto: str) -> tuple[str, int]:
    """`OPTIONS (password 'x', user 'y')`: o valor das opções de conexão vira marcador."""
    total = 0

    def corpo(m: re.Match[str]) -> str:
        nonlocal total
        novo, n = _RE_OPCAO.subn(
            lambda o: f"{o['chave']} {o['q']}{marcador('credencial')}{o['q']}", m["corpo"]
        )
        total += n
        return m.group(0).replace(m["corpo"], novo) if n else m.group(0)

    return _RE_OPTIONS.sub(corpo, texto), total


# ----------------------------------------------------------------- texto


class Mascarador:
    """Aplica a máscara mantendo o mapa valor -> N (chave: só os dígitos) de CPF e CNPJ."""

    def __init__(self, mapa: dict[str, dict[str, int]] | None = None) -> None:
        mapa = mapa or {}
        self.mapa: dict[str, dict[str, int]] = {t: dict(mapa.get(t, {})) for t in NUMERADOS}
        self.alterado = False

    def _numero(self, tipo: str, valor: str) -> int:
        chave = "".join(c for c in valor if c.isdigit())
        tabela = self.mapa[tipo]
        if chave not in tabela:
            tabela[chave] = max(tabela.values(), default=0) + 1
            self.alterado = True
        return tabela[chave]

    def mascarar(self, texto: str) -> tuple[str, dict[str, int]]:
        """Devolve (texto mascarado, contagem por tipo). Idempotente."""
        contagem: Counter[str] = Counter()
        for tipo in TIPOS:
            if tipo == "credencial":
                texto, n = _mascarar_credenciais(texto)
                if n:
                    contagem[tipo] += n
                continue
            valido = _VALIDADORES[tipo]

            def trocar(
                m: re.Match[str], tipo: str = tipo, valido: Callable[[str], bool] = valido
            ) -> str:
                if not valido(m.group(0)):
                    return m.group(0)
                contagem[tipo] += 1
                return marcador(tipo, self._numero(tipo, m.group(0)) if tipo in NUMERADOS else None)

            texto = _REGEX[tipo].sub(trocar, texto)
        return texto, dict(sorted(contagem.items()))


def mascarar(texto: str, mascarador: Mascarador | None = None) -> tuple[str, dict[str, int]]:
    """Máscara de um texto; sem `mascarador`, a numeração vale só para esta chamada."""
    return (mascarador or Mascarador()).mascarar(texto)


def encontrar(texto: str) -> dict[str, int]:
    """Contagem por tipo das ocorrências NÃO mascaradas em `texto` (nunca os valores)."""
    return mascarar(texto)[1]


# ----------------------------------------------------------------- documentos


class _Aplicador:
    """Callable (caminho, texto) -> texto; guarda a contagem agregada, sem valores."""

    def __init__(self, mascarador: Mascarador) -> None:
        self.mascarador = mascarador
        self.agregado: Counter[tuple[str, str, str]] = Counter()

    def nome_publico(self, nome: str) -> str:
        """Nome mascarado e sem N, para compor caminhos de auditoria."""
        return sem_numero(self.mascarador.mascarar(nome)[0])

    def __call__(self, caminho: str, texto: str) -> str:
        novo, contagem = self.mascarador.mascarar(texto)
        for tipo, n in contagem.items():
            objeto, campo = _objeto_e_campo(caminho)
            self.agregado[(objeto, campo, tipo)] += n
        return novo


def _percorrer(valor: Any, caminho: str, aplicar: Callable[[str, str], str], rotular: Any) -> Any:
    if isinstance(valor, str):
        return aplicar(caminho, valor)
    if isinstance(valor, dict):
        return {
            k: _percorrer(v, f"{caminho}.{k}" if caminho else k, aplicar, rotular)
            for k, v in valor.items()
        }
    if isinstance(valor, list):
        return [
            _percorrer(v, f"{caminho}{_rotulo_item(v, i, rotular)}", aplicar, rotular)
            for i, v in enumerate(valor)
        ]
    return valor


def _rotulo_item(item: Any, indice: int, rotular: Any) -> str:
    if isinstance(item, dict) and isinstance(item.get("nome"), str):
        return f"[{rotular(item['nome'])}]"
    return f"[{indice}]"


_CATEGORIAS = {"relacoes": "relacao", "funcoes": "funcao", "sequencias": "sequencia", "tipos": "tipo"}


def _objeto_e_campo(caminho: str) -> tuple[str, str]:
    """Separa 'relacoes[venda].colunas[id].padrao' em ('relacao:venda', 'colunas[id].padrao')."""
    m = re.match(r"^(relacoes|funcoes|sequencias|tipos)\[((?:[^\]]|\](?!\.|$))*)\]\.?(.*)$", caminho)
    if not m:
        return "schema", caminho
    return f"{_CATEGORIAS[m.group(1)]}:{m.group(2)}", m.group(3)


def _chave(*valores: Any) -> tuple[str, ...]:
    return tuple("" if v is None else str(v) for v in valores)


def reordenar(estrutura: dict[str, Any]) -> dict[str, Any]:
    """Reordena as listas pelo nome já mascarado (o que o leitor do arquivo enxerga)."""
    estrutura["relacoes"].sort(key=lambda r: _chave(r["nome"]))
    for r in estrutura["relacoes"]:
        r["depende_de"].sort(key=lambda d: _chave(d["schema"], d["relacao"]))
        for lista in ("restricoes", "indices", "gatilhos"):
            r[lista].sort(key=lambda x: _chave(x["nome"]))
    estrutura["funcoes"].sort(key=lambda f: _chave(f["nome"], f["argumentos_identidade"]))
    estrutura["sequencias"].sort(key=lambda x: _chave(x["nome"]))
    estrutura["tipos"].sort(key=lambda x: _chave(x["nome"]))
    return estrutura


def verificar_nomes_unicos(estrutura: dict[str, Any]) -> None:
    """Falha se, após mascarar, houver nome repetido por schema e tipo de objeto."""
    grupos: dict[str, list[tuple[str, ...]]] = {
        "relacao": [_chave(r["nome"]) for r in estrutura["relacoes"]],
        "funcao": [_chave(f["nome"], f["argumentos_identidade"]) for f in estrutura["funcoes"]],
        "sequencia": [_chave(x["nome"]) for x in estrutura["sequencias"]],
        "tipo": [_chave(x["nome"]) for x in estrutura["tipos"]],
    }
    for rotulo, lista in (("coluna", "colunas"), ("restricao", "restricoes"),
                          ("indice", "indices"), ("gatilho", "gatilhos")):  # fmt: skip
        grupos[rotulo] = [_chave(r["nome"], x["nome"]) for r in estrutura["relacoes"] for x in r[lista]]
    for rotulo, nomes in grupos.items():
        duplicados = len(nomes) - len(set(nomes))
        if duplicados:
            raise ErroMascara(
                f"schema '{estrutura.get('schema')}': {duplicados} nome(s) de {rotulo} "
                "duplicado(s) após o mascaramento."
            )


def mascarar_estrutura(
    estrutura: dict[str, Any], mascarador: Mascarador | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Mascara todo texto da estrutura de um schema, identificadores inclusive.

    Devolve (documento reordenado, registros). Cada registro:
    {schema, objeto, campo, tipo, quantidade}, ordenado e sem valores nem N.
    """
    schema = str(estrutura.get("schema", ""))
    aplicador = _Aplicador(mascarador or Mascarador())
    novo = reordenar(_percorrer(estrutura, "", aplicador, aplicador.nome_publico))
    verificar_nomes_unicos(novo)
    registros = [
        {"schema": schema, "objeto": o, "campo": c, "tipo": t, "quantidade": n}
        for (o, c, t), n in sorted(aplicador.agregado.items())
    ]
    return novo, registros


def varrer_documento(documento: Any) -> list[tuple[str, dict[str, int]]]:
    """Ocorrências não mascaradas num documento JSON: lista de (caminho, contagem por tipo)."""
    achados: list[tuple[str, dict[str, int]]] = []

    def aplicar(caminho: str, texto: str) -> str:
        contagem = encontrar(texto)
        if contagem:
            achados.append((sem_numero(mascarar(caminho)[0]), contagem))
        return texto

    _percorrer(documento, "", aplicar, lambda nome: sem_numero(mascarar(nome)[0]))
    return achados
