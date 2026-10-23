"""Análise dos joins de um comando SQL com sqlglot (decisão D-018, itens 3 a 5).

Offline e genérico. Recebe o texto de um comando e o índice do catálogo; devolve ligações
(origem -> destino, em pares de colunas) e contagens de descarte. Nunca guarda trecho de código.

Regras principais:
- escopos: SELECT (CTEs, subconsultas, UNION), UPDATE ... FROM, DELETE ... USING e INSERT ... SELECT;
  uma subconsulta correlacionada enxerga os aliases dos escopos externos;
- igualdades `coluna = coluna` (as colunas podem estar dentro de CAST e parênteses) entre aliases
  diferentes, em JOIN ON, USING e WHERE, só nos termos ligados por AND no nível de cima;
- as duas colunas precisam estar qualificadas (exceto em USING) e existir na relação resolvida;
- o lado cujo conjunto de colunas é uma chave da relação (PK, UNIQUE ou índice único não parcial
  e sem expressão) é o destino; com tenant declarado, também se compara a chave sem o tenant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

import sqlglot
from sqlglot import exp

NIVEL_PK = 2
NIVEL_UNICA = 1
TIPOS_VIEW = ("view", "materializada")
TIPOS_CHAVE = {"pk": NIVEL_PK, "uk": NIVEL_UNICA, "unique": NIVEL_UNICA}
NOTA_UM_PARA_UM = "1:1: direção não determinada pelo código"
DESCARTES = (
    "sem_chave",
    "lado_view",
    "lado_nao_relacao",
    "lado_nao_resolvido",
    "coluna_nao_qualificada",
    "coluna_inexistente",
    "autojuncao_trivial",
    "tenant_ignorada",
)

Chave = tuple[str, str]  # schema, relação


# ----------------------------------------------------------------- catálogo


@dataclass
class InfoRelacao:
    tipo: str
    colunas: dict[str, str]
    chaves: list[tuple[int, tuple[str, ...]]]
    pai: Chave | None


class IndiceCatalogo:
    """Relações, colunas, chaves e partições do catálogo, para resolver nomes e achar destinos."""

    def __init__(self, estruturas: dict[str, dict[str, Any]]) -> None:
        self.relacoes: dict[Chave, InfoRelacao] = {}
        self.por_nome: dict[str, list[str]] = {}
        for schema in sorted(estruturas):
            for rel in estruturas[schema].get("relacoes", []):
                self._adicionar(schema, rel)

    def _adicionar(self, schema: str, rel: dict[str, Any]) -> None:
        colunas = {c["nome"]: c.get("tipo") or "" for c in rel.get("colunas", [])}
        chaves: list[tuple[int, tuple[str, ...]]] = []
        for r in rel.get("restricoes", []):
            nivel = TIPOS_CHAVE.get(r.get("tipo", ""))
            if nivel is not None and r.get("colunas"):
                chaves.append((nivel, tuple(r["colunas"])))
        for i in rel.get("indices", []):
            cols = i.get("colunas") or []
            if i.get("unico") and not i.get("parcial") and cols and all(c in colunas for c in cols):
                chaves.append((NIVEL_UNICA, tuple(cols)))
        part = rel.get("particao")
        pai = (part["pai_schema"], part["pai_nome"]) if isinstance(part, dict) and part.get("pai_nome") else None
        self.relacoes[(schema, rel["nome"])] = InfoRelacao(rel.get("tipo", ""), colunas, chaves, pai)
        self.por_nome.setdefault(rel["nome"], []).append(schema)

    def resolver(self, schema: str | None, nome: str, schema_objeto: str) -> Chave | None:
        """Nome qualificado: direto. Não qualificado: schema do objeto, depois nome único no catálogo."""
        if schema is not None:
            return (schema, nome) if (schema, nome) in self.relacoes else None
        if (schema_objeto, nome) in self.relacoes:
            return (schema_objeto, nome)
        schemas = self.por_nome.get(nome, [])
        return (schemas[0], nome) if len(schemas) == 1 else None

    def raiz(self, chave: Chave) -> Chave:
        """Uma partição vira a tabela particionada."""
        pai = self.relacoes[chave].pai
        return pai if pai is not None and pai in self.relacoes else chave

    def casar(self, chave: Chave, colunas: set[str], tenant: str | None) -> tuple[int, bool] | None:
        """(nível da chave, casou só sem o tenant) se o conjunto de colunas é uma chave; senão `None`."""
        chaves = self.relacoes[chave].chaves
        exatas = [n for n, k in chaves if set(k) == colunas]
        if exatas:
            return max(exatas), False
        if tenant is not None:
            sem = [n for n, k in chaves if tenant in k and len(k) > 1 and set(k) - {tenant} == colunas]
            if sem:
                return max(sem), True
        return None


# ----------------------------------------------------------------- resultado


@dataclass(frozen=True)
class Ligacao:
    """Aresta candidata: pares de colunas ordenados pelo nome da coluna de origem."""

    origem: Chave
    colunas_origem: tuple[str, ...]
    destino: Chave
    colunas_destino: tuple[str, ...]
    tipos_iguais: bool
    chave_sem_tenant: bool
    um_para_um: bool


def contadores_zerados() -> dict[str, int]:
    return {"igualdades": 0, **{d: 0 for d in DESCARTES}}


# ----------------------------------------------------------------- sqlglot


def _nome(ident: Any) -> str | None:
    """Identificador sem aspas em minúsculas; com aspas, como está."""
    if not isinstance(ident, exp.Identifier):
        return None
    texto = str(ident.this)
    return texto if ident.args.get("quoted") else texto.lower()


def _desembrulhar(no: Any) -> Any:
    while isinstance(no, (exp.Paren, exp.Cast)):  # TryCast herda de Cast
        no = no.this
    return no


def _conjuntos(no: Any) -> list[Any]:
    """Termos ligados por AND no nível de cima; o que está dentro de OR fica fora."""
    if isinstance(no, exp.And):
        return _conjuntos(no.this) + _conjuntos(no.expression)
    if isinstance(no, exp.Paren) and isinstance(no.this, exp.And):
        return _conjuntos(no.this)
    return [no]


def _arg(no: Any, *nomes: str) -> Any:
    for n in nomes:
        valor = no.args.get(n)
        if valor:
            return valor
    return None


def _fonte_do_from(no: Any) -> Any:
    origem = _arg(no, "from_", "from")
    return origem.this if origem else None


def _ordem(fonte: "Fonte", colunas: list[str]) -> tuple[str, tuple[str, ...]]:
    """Ordenação do 1:1: `schema.relacao` e, no empate, as colunas."""
    return f"{fonte.chave[0]}.{fonte.chave[1]}", tuple(sorted(colunas))  # type: ignore[index]


@dataclass(eq=False)
class Fonte:
    uid: int
    classe: str  # relacao | view | nao_relacao | nao_resolvida
    chave: Chave | None = None


Escopo = dict[str, Fonte]


class AnalisadorDeJoins:
    """Analisa um comando; as contagens só valem se `analisar` terminar sem exceção."""

    def __init__(self, indice: IndiceCatalogo, tenant: str | None) -> None:
        self.indice = indice
        self.tenant = tenant

    # -- entrada ---------------------------------------------------------

    def analisar(self, comando: str, schema_objeto: str) -> tuple[list[Ligacao], dict[str, int]]:
        arvores = sqlglot.parse(comando, read="postgres")
        return self.analisar_arvores(arvores, schema_objeto)

    def analisar_arvores(self, arvores: list[Any], schema_objeto: str) -> tuple[list[Ligacao], dict[str, int]]:
        self._schema = schema_objeto
        self._cont = contadores_zerados()
        self._grupos: dict[tuple[int, int], dict[str, Any]] = {}
        self._uid = 0
        for arvore in arvores:
            if arvore is not None:
                self._processar(arvore, {}, frozenset())
        return self._resolver_grupos(), self._cont

    # -- escopos ---------------------------------------------------------

    def _processar(self, no: Any, visiveis: Escopo, ctes: frozenset[str]) -> None:
        if isinstance(no, exp.Select):
            self._select(no, visiveis, ctes)
        elif isinstance(no, exp.SetOperation):
            ctes = self._com(no, visiveis, ctes)
            self._processar(no.this, visiveis, ctes)
            self._processar(no.expression, visiveis, ctes)
        elif isinstance(no, exp.Update):
            self._dml(no, [no.this, _fonte_do_from(no)], visiveis, ctes)
        elif isinstance(no, exp.Delete):
            usando = no.args.get("using") or []
            self._dml(no, [no.this, *usando], visiveis, ctes)
        else:  # INSERT ... SELECT, CREATE ... AS e o resto: só procura as consultas dentro
            ctes = self._com(no, visiveis, ctes)
            for filho in self._consultas_dentro(no):
                self._processar(filho, visiveis, ctes)

    def _com(self, no: Any, visiveis: Escopo, ctes: frozenset[str]) -> frozenset[str]:
        """Processa os corpos das CTEs e devolve os nomes de CTE visíveis."""
        clausula = _arg(no, "with_", "with")
        if clausula is None:
            return ctes
        for cte in clausula.expressions:
            nome = _nome(cte.args["alias"].this) if cte.args.get("alias") else None
            if nome is not None:
                ctes = ctes | {nome}
            self._processar(cte.this, visiveis, ctes)
        return ctes

    def _consultas_dentro(self, no: Any) -> Iterator[Any]:
        """Consultas mais próximas dentro do nó (sem descer nelas e sem as CTEs, tratadas à parte)."""
        for chave, valor in no.args.items():
            if chave in ("with", "with_"):
                continue
            for filho in valor if isinstance(valor, list) else [valor]:
                if isinstance(filho, (exp.Select, exp.SetOperation, exp.Update, exp.Delete)):
                    yield filho
                elif isinstance(filho, exp.Expression):
                    yield from self._consultas_dentro(filho)

    def _select(self, no: exp.Select, visiveis: Escopo, ctes: frozenset[str]) -> None:
        self._dml(no, [_fonte_do_from(no)], visiveis, ctes)

    def _dml(self, no: Any, primarias: list[Any], visiveis: Escopo, ctes: frozenset[str]) -> None:
        """Escopo de SELECT, UPDATE ou DELETE: fontes, igualdades e consultas aninhadas."""
        ctes = self._com(no, visiveis, ctes)
        fontes: list[tuple[Fonte, Any]] = []  # (fonte, join ou None) na ordem de aparição
        local: Escopo = {}
        juncoes = list(no.args.get("joins") or [])
        for primaria in primarias:
            if primaria is None:
                continue
            juncoes += primaria.args.get("joins") or []
            self._registrar_fonte(primaria, None, fontes, local, ctes)
        for j in juncoes:
            self._registrar_fonte(j.this, j, fontes, local, ctes)
        escopo = {**visiveis, **local}
        termos: list[Any] = []
        for _, j in fontes:
            if j is not None and j.args.get("on") is not None:
                termos += _conjuntos(j.args["on"])
        where = no.args.get("where")
        if where is not None:
            termos += _conjuntos(where.this)
        for termo in termos:
            self._igualdade(termo, escopo)
        for i, (fonte, j) in enumerate(fontes):
            if j is not None and j.args.get("using"):
                for ident in j.args["using"]:
                    self._using(_nome(ident.this if isinstance(ident, exp.Column) else ident), fonte, fontes[:i])
        for filho in self._consultas_dentro(no):
            self._processar(filho, escopo, ctes)

    # -- fontes ----------------------------------------------------------

    def _nova(self, classe: str, chave: Chave | None = None) -> Fonte:
        self._uid += 1
        return Fonte(self._uid, classe, chave)

    def _registrar_fonte(
        self, no: Any, junta: Any, fontes: list[tuple[Fonte, Any]], local: Escopo, ctes: frozenset[str]
    ) -> None:
        alias_no = no.args.get("alias") if isinstance(no, exp.Expression) else None
        alias = _nome(alias_no.this) if alias_no is not None and alias_no.this is not None else None
        if isinstance(no, exp.Table) and isinstance(no.this, exp.Identifier):
            nome = _nome(no.this) or ""
            schema = _nome(no.args.get("db"))
            fonte = self._fonte_de_tabela(schema, nome, ctes)
            alias = alias or nome
        else:
            fonte = self._nova("nao_relacao")
        if alias is not None:
            local[alias] = fonte
        fontes.append((fonte, junta))

    def _fonte_de_tabela(self, schema: str | None, nome: str, ctes: frozenset[str]) -> Fonte:
        if schema is None and nome in ctes:
            return self._nova("nao_relacao")
        chave = self.indice.resolver(schema, nome, self._schema)
        if chave is None:
            return self._nova("nao_resolvida")
        chave = self.indice.raiz(chave)
        classe = "view" if self.indice.relacoes[chave].tipo in TIPOS_VIEW else "relacao"
        return self._nova(classe, chave)

    # -- igualdades ------------------------------------------------------

    def _igualdade(self, termo: Any, escopo: Escopo) -> None:
        if not isinstance(termo, exp.EQ):
            return
        esq, dir_ = _desembrulhar(termo.this), _desembrulhar(termo.expression)
        if not (isinstance(esq, exp.Column) and isinstance(dir_, exp.Column)):
            return
        qual_e, qual_d = _nome(esq.args.get("table")), _nome(dir_.args.get("table"))
        if qual_e is not None and qual_e == qual_d:
            return  # mesmo alias dos dois lados: não é um join
        self._cont["igualdades"] += 1
        if qual_e is None or qual_d is None:
            self._cont["coluna_nao_qualificada"] += 1
            return
        self._par(escopo.get(qual_e), _nome(esq.this), escopo.get(qual_d), _nome(dir_.this))

    def _using(self, coluna: str | None, direita: Fonte, anteriores: list[tuple[Fonte, Any]]) -> None:
        if coluna is None:
            return
        self._cont["igualdades"] += 1
        esquerda = None
        for fonte, _ in reversed(anteriores):
            if fonte.classe == "relacao" and fonte.chave and coluna in self.indice.relacoes[fonte.chave].colunas:
                esquerda = fonte
                break
        if esquerda is None and anteriores:
            esquerda = anteriores[-1][0]
        self._par(esquerda, coluna, direita, coluna)

    def _par(self, fe: Fonte | None, ce: str | None, fd: Fonte | None, cd: str | None) -> None:
        for fonte in (fe, fd):
            if fonte is None or fonte.classe == "nao_resolvida":
                descarte = "lado_nao_resolvido"
            elif fonte.classe == "nao_relacao":
                descarte = "lado_nao_relacao"
            elif fonte.classe == "view":
                descarte = "lado_view"
            else:
                continue
            self._cont[descarte] += 1
            return
        assert fe is not None and fd is not None and fe.chave and fd.chave and ce and cd
        if ce not in self.indice.relacoes[fe.chave].colunas or cd not in self.indice.relacoes[fd.chave].colunas:
            self._cont["coluna_inexistente"] += 1
            return
        if fe.chave == fd.chave and ce == cd:
            self._cont["autojuncao_trivial"] += 1
            return
        if self.tenant is not None and ce == self.tenant and cd == self.tenant:
            self._cont["tenant_ignorada"] += 1
            return
        if fe.uid > fd.uid:
            fe, ce, fd, cd = fd, cd, fe, ce
        grupo = self._grupos.setdefault((fe.uid, fd.uid), {"a": fe, "b": fd, "pares": set()})
        grupo["pares"].add((ce, cd))

    # -- direção e chave -------------------------------------------------

    def _resolver_grupos(self) -> list[Ligacao]:
        ligacoes: list[Ligacao] = []
        for chave in sorted(self._grupos):
            grupo = self._grupos[chave]
            ligacao = self._ligacao(grupo["a"], grupo["b"], sorted(grupo["pares"]))
            if ligacao is None:
                self._cont["sem_chave"] += 1
            else:
                ligacoes.append(ligacao)
        return ligacoes

    def _ligacao(self, a: Fonte, b: Fonte, pares: list[tuple[str, str]]) -> Ligacao | None:
        assert a.chave and b.chave
        casa_a = self.indice.casar(a.chave, {p[0] for p in pares}, self.tenant)
        casa_b = self.indice.casar(b.chave, {p[1] for p in pares}, self.tenant)
        if casa_a is None and casa_b is None:
            return None
        um_para_um = False
        if casa_a is not None and casa_b is not None:
            if casa_a[0] == casa_b[0]:  # dois lados chave: uma aresta só, origem = menor schema.relacao
                um_para_um = True
                a_eh_origem = _ordem(a, [p[0] for p in pares]) <= _ordem(b, [p[1] for p in pares])
            else:  # a PK vence a chave única
                a_eh_origem = casa_a[0] < casa_b[0]
        else:
            a_eh_origem = casa_a is None
        if a_eh_origem:
            origem, destino, par_o_d, casa_dest = a, b, list(pares), casa_b
        else:
            origem, destino, par_o_d, casa_dest = b, a, [(y, x) for x, y in pares], casa_a
        par_o_d.sort()
        tipos_o = self.indice.relacoes[origem.chave].colunas  # type: ignore[index]
        tipos_d = self.indice.relacoes[destino.chave].colunas  # type: ignore[index]
        return Ligacao(
            origem=origem.chave,  # type: ignore[arg-type]
            colunas_origem=tuple(p[0] for p in par_o_d),
            destino=destino.chave,  # type: ignore[arg-type]
            colunas_destino=tuple(p[1] for p in par_o_d),
            tipos_iguais=all(tipos_o[o] == tipos_d[d] for o, d in par_o_d),
            chave_sem_tenant=bool(casa_dest and casa_dest[1]),
            um_para_um=um_para_um,
        )


