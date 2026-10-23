<!-- pg-atlas:auto inicio secao="cabecalho" -->
# {schema}.{rotina}({argumentos})

> {function | procedure | agregado | função de janela | função de gatilho} · linguagem {plpgsql | sql | ...} · retorna {tipo} · {volátil | estável | imutável} · {SECURITY DEFINER, se houver} · acesso: {com_execute | sem_execute}

Comentário do banco: {comentário, se houver} [declarado]
<!-- pg-atlas:auto fim -->

## Objetivo

<!-- pg-atlas:pendente fase="4" -->
_Pendente: a preencher na fase 4._

{Depois de preenchido: o que a rotina faz, em 2 a 4 frases, em termos de negócio.} [{grau}]

## Parâmetros

<!-- pg-atlas:pendente fase="4" -->
_Pendente: a preencher na fase 4._

| Parâmetro | Tipo | Significado |
|---|---|---|
| {nome} | {tipo} | {significado} |

## Retorno

<!-- pg-atlas:pendente fase="4" -->
_Pendente: a preencher na fase 4._

{Depois de preenchido: o que volta e em que formato. Se retorna JSON ou conjunto de linhas, descrever a estrutura.}

<!-- pg-atlas:auto inicio secao="dependencias" -->
## Dependências

| Tipo | Objeto | Como |
|---|---|---|
| lê | {schema.tabela} | SELECT |
| escreve | {schema.tabela} | INSERT / UPDATE / DELETE |
| chama | {schema.rotina} | |
| é chamada por gatilho | {gatilho em schema.tabela} | {pg_get_triggerdef} |
| SQL dinâmico | sim/não | {trecho que monta o SQL} |
<!-- pg-atlas:auto fim -->

## Regras de negócio

<!-- pg-atlas:pendente fase="4" -->
_Pendente: a preencher na fase 4._

- {regra implementada pela rotina, com a referência às linhas do código} [{grau}]

## Efeitos colaterais e cuidados

<!-- pg-atlas:pendente fase="4" -->
_Pendente: a preencher na fase 4._

- {o que muda no banco ao executar; se é seguro chamar numa consulta de leitura}

<!-- pg-atlas:auto inicio secao="codigo" -->
## Código

```sql
{definição completa}
```
<!-- pg-atlas:auto fim -->

## Histórico

- {data}: ficha criada pela fase {n}.
