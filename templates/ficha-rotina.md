# {schema}.{rotina}({argumentos})

> {function | procedure | agregado | gatilho} · linguagem {plpgsql | sql | ...} · retorna {tipo} · {volatile | stable | immutable} · {SECURITY DEFINER, se houver}

## Objetivo

{O que a rotina faz, em 2 a 4 frases, em termos de negócio.} [{grau}]

## Parâmetros

| Parâmetro | Tipo | Significado |
|---|---|---|
| {nome} | {tipo} | {significado} |

## Retorno

{O que volta e em que formato. Se retorna JSON ou conjunto de linhas, descrever a estrutura.}

<!-- pg-atlas:auto inicio secao="dependencias" -->
## Dependências

| Tipo | Objeto | Como |
|---|---|---|
| lê | {schema.tabela} | SELECT |
| escreve | {schema.tabela} | INSERT / UPDATE / DELETE |
| chama | {schema.rotina} | |
| é chamada por | {schema.rotina ou gatilho} | |
| SQL dinâmico | sim/não | {trecho que monta o SQL} |
<!-- pg-atlas:auto fim -->

## Regras de negócio

- {regra implementada pela rotina, com a referência às linhas do código} [{grau}]

## Efeitos colaterais e cuidados

- {o que muda no banco ao executar; se é seguro chamar numa consulta de leitura}

<!-- pg-atlas:auto inicio secao="codigo" -->
## Código

```sql
{definição completa}
```
<!-- pg-atlas:auto fim -->
