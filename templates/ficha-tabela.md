# {schema}.{tabela}

> {tipo: tabela | view | view materializada | tabela particionada} · {linhas_estimadas} linhas (estimativa) · {tamanho} · triagem: {classe} · acesso: {com_select | sem_select}

## Propósito

{O que esta tabela representa no negócio, em 2 a 4 frases. Qual é a granularidade: uma linha representa o quê.} [{grau}]

## Como usar

- **Filtro obrigatório:** {por exemplo, filtro de tenant, filtro de situação ativa, vigência} [{grau}]
- **Chave de negócio:** {colunas que identificam a linha para quem consulta} [{grau}]
- **Armadilhas:** {formatos inesperados, campos livres, duplicidades conhecidas} [{grau}]

<!-- pg-atlas:auto inicio secao="colunas" -->
## Colunas

| # | Coluna | Tipo | Nulo | Default | Chave | Nulos (%) | Distintos |
|---|---|---|---|---|---|---|---|
| 1 | {coluna} | {tipo} | {sim/não} | {default} | {PK/FK/UK} | {null_frac} | {n_distinct} |
<!-- pg-atlas:auto fim -->

## Significado das colunas

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
| {nome} | {PK/UK/índice/check} | {definição} |
<!-- pg-atlas:auto fim -->

<!-- pg-atlas:auto inicio secao="rotinas" -->
## Rotinas e gatilhos

| Rotina | Relação com a tabela |
|---|---|
| {schema.rotina} | {lê / escreve / gatilho BEFORE INSERT} |
<!-- pg-atlas:auto fim -->

## Consultas típicas

```sql
-- {o que a consulta responde}
SELECT ...
```

Ver também: {links para receitas relacionadas}

## Histórico

- {data}: ficha criada pela fase {n}.
