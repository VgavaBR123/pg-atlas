---
tipo: tabela                 # schema | tabela | rotina | receita | conceito
schema: {schema}
assunto: {assunto}
confianca: {confirmado | hipotese}
atualizado_em: {AAAA-MM-DD}
ficha_completa: "schemas/{schema}/tabelas/{tabela}.md"
tags: [tipo/tabela, assunto/{assunto-slug}]
---

# {schema}.{tabela}

Pertence a: [[Schemas/{schema}]]

## O que é

{2 a 3 frases: o que a tabela guarda e o que é uma linha.}

## Quando usar

{Para quais perguntas esta tabela é o ponto de partida.}

## Filtros obrigatórios

- {filtro de tenant, situação, vigência}

## Armadilhas

- {até 3, as mais importantes}

## Consulta de exemplo

```sql
{consulta curta, com os filtros obrigatórios}
```

## Relacionado

- [[Tabelas/{schema}.{outra_tabela}]]: {em uma linha, como se ligam}

---
Ficha completa: `{ficha_completa}` no repositório de documentação.
