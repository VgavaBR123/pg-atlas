<!-- pg-atlas:auto inicio secao="cabecalho" -->
# {schema}.{tabela}

> {tipo: tabela | tabela particionada | partição | view | view materializada | tabela estrangeira} · {linhas_estimadas} linhas (estimativa) · {tamanho} · triagem: {classe} · acesso: {com_select | sem_select} · {partição de [mãe](mae.md), só em partição}

Comentário do banco: {comentário, se houver} [declarado]
<!-- pg-atlas:auto fim -->

## Propósito

<!-- pg-atlas:pendente fase="3" -->
_Pendente: a preencher na fase 3._

{Depois de preenchido: o que esta tabela representa no negócio, em 2 a 4 frases. Qual é a granularidade: uma linha representa o quê.} [{grau}]

## Como usar

<!-- pg-atlas:pendente fase="3" -->
_Pendente: a preencher na fase 3._

- **Filtro obrigatório:** {por exemplo, filtro de tenant, filtro de situação ativa, vigência} [{grau}]
- **Chave de negócio:** {colunas que identificam a linha para quem consulta} [{grau}]
- **Armadilhas:** {formatos inesperados, campos livres, duplicidades conhecidas} [{grau}]

<!-- pg-atlas:auto inicio secao="colunas" -->
## Colunas

| # | Coluna | Tipo | Nulo | Default | Chave | Nulos (%) | Distintos |
|---|---|---|---|---|---|---|---|
| 1 | {coluna} | {tipo} | {sim/não} | {default} | {PK/UK/FK} | {null_frac} | {n_distinct} |
<!-- pg-atlas:auto fim -->

## Significado das colunas

<!-- pg-atlas:pendente fase="3" -->
_Pendente: a preencher na fase 3._

| Coluna | Significado | Grau |
|---|---|---|
| {coluna} | {o que significa, unidade, formato, domínio} | {grau} |

<!-- pg-atlas:auto inicio secao="relacionamentos" -->
## Relacionamentos

**Esta tabela aponta para:**

| Coluna(s) | Tabela de destino | Grau | Casamento |
|---|---|---|---|
| {coluna} | {schema.tabela(coluna)} | {grau} | {taxa} |

**Apontam para esta tabela:**

| Tabela de origem | Coluna(s) | Grau |
|---|---|---|
| {schema.tabela} | {coluna} | {grau} |
<!-- pg-atlas:auto fim -->

<!-- pg-atlas:auto inicio secao="indices" -->
## Índices e restrições

| Nome | Tipo | Definição |
|---|---|---|
| {nome} | {PK/UK/CHECK/índice/índice único} | {definição} |
<!-- pg-atlas:auto fim -->

<!-- pg-atlas:auto inicio secao="rotinas" -->
## Rotinas e gatilhos

| Gatilho | Momento e evento | Função de gatilho |
|---|---|---|
| {nome} | {BEFORE INSERT OR UPDATE (linha)} | {schema.função} |
<!-- pg-atlas:auto fim -->

<!-- pg-atlas:auto inicio secao="dependencias" -->
## Dependências

### Depende de

(só em view e view materializada)

| Objeto | Tipo |
|---|---|
| {schema.relação} | {tabela | view | ...} |

### Views que dependem desta

| View | Tipo |
|---|---|
| {schema.view} | {view | view materializada} |
<!-- pg-atlas:auto fim -->

<!-- pg-atlas:auto inicio secao="definicao" -->
## Definição

(só em view e view materializada)

```sql
{pg_get_viewdef}
```
<!-- pg-atlas:auto fim -->

<!-- pg-atlas:auto inicio secao="particoes" -->
## Partições

(só em tabela particionada)

| Partição | Linhas (est.) | Limite |
|---|---|---|
| {schema.partição} | {linhas} | {FOR VALUES ...} |
<!-- pg-atlas:auto fim -->

## Consultas típicas

<!-- pg-atlas:pendente fase="3" -->
_Pendente: a preencher na fase 3._

```sql
-- {o que a consulta responde}
SELECT ...
```

Ver também: {links para receitas relacionadas}

## Histórico

- {data}: ficha criada pela fase {n}.
