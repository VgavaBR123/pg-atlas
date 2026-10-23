<!-- pg-atlas:auto inicio secao="cabecalho" -->
# Relatório da fase 1: catálogo

**Projeto:** {nome do projeto}
**Catálogo extraído em:** {extraido_em de catalogo/_banco.json}
<!-- pg-atlas:auto fim -->

<!-- pg-atlas:auto inicio secao="banco" -->
## Números do banco

Servidor: {versão}. Tamanho total do banco: {tamanho}.

| Item | Quantidade |
|---|---|
| Schemas | {n} |
| Tabelas (inclui {n} partições) | {n} |
| Tabelas particionadas · Views · Views materializadas · Tabelas estrangeiras | {n} |
| Funções · Procedimentos (procedures) · Agregações e outras rotinas | {n} |
| Gatilhos · Sequências · Índices · Restrições · Colunas · Tipos definidos | {n} |
<!-- pg-atlas:auto fim -->

<!-- pg-atlas:auto inicio secao="triagem" -->
## Distribuição da triagem

| Classe | Tabelas | Parte |
|---|---|---|
| {classe, na ordem de precedência} | {n} | {%} |
<!-- pg-atlas:auto fim -->

<!-- pg-atlas:auto inicio secao="maiores" -->
## Maiores tabelas (top 20)

| # | Schema | Tabela | Tipo | Tamanho total | Linhas estimadas |
|---|---|---|---|---|---|
| {n} | {schema} | {tabela} | {tipo} | {tamanho} | {linhas, 2 algarismos significativos} |
<!-- pg-atlas:auto fim -->

<!-- pg-atlas:auto inicio secao="sem_acesso" -->
## Objetos sem acesso

### Relações sem permissão de SELECT

| Schema | Tipo | Quantidade |
|---|---|---|
| {schema} | {tipo} | {n} |

### Rotinas sem permissão de EXECUTE

Nenhum.
<!-- pg-atlas:auto fim -->

<!-- pg-atlas:auto inicio secao="mascaramento" -->
## Mascaramento aplicado ao catálogo

| Tipo | Ocorrências mascaradas |
|---|---|
| {tipo} | {n} |
<!-- pg-atlas:auto fim -->

<!-- pg-atlas:auto inicio secao="verificacao" -->
## Verificação do catálogo

| Verificação | Resultado | Resumo |
|---|---|---|
| {nome} | OK ou FALHA | {contagens} |
<!-- pg-atlas:auto fim -->

<!-- pg-atlas:auto inicio secao="tempo" -->
## Tempo de execução

| Item | Início | Fim | Tempo de relógio | Horas de execução |
|---|---|---|---|---|
| Fase 1 | {data hora} | {data hora ou "em andamento até ..."} | {duração} | {horas} |
| Tarefa {n.n} | {data hora} | {data hora} | {duração} | {horas} |
<!-- pg-atlas:auto fim -->

<!-- pg-atlas:auto inicio secao="custo" -->
## Custo da fase

| Modelo | Entrada | Saída | Cache (escrita) | Cache (leitura) | Equivalente US$ |
|---|---|---|---|---|---|
| {modelo} | {tokens} | {tokens} | {tokens} | {tokens} | {valor ou "preço não informado"} |
<!-- pg-atlas:auto fim -->

## Observações

_Texto escrito à mão (só metadado, sem valores de dado). O comando `relatorio catalogo` nunca reescreve esta seção._
