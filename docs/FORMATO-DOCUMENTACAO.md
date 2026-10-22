# Formato da documentação gerada

Este documento define como a documentação do banco é organizada e escrita. Vale para o esqueleto mecânico (fase 1) e para o conteúdo escrito pelos agentes (fases 3 a 5).

## Como costuma ser uma documentação de banco de dados

Uma documentação de banco madura costuma ter quatro camadas:

1. **Dicionário de dados:** cada tabela e coluna com tipo, nulidade, chave e descrição. É o que ferramentas como tbls, SchemaSpy e Dataedo geram.
2. **Modelo de relacionamentos:** como as tabelas se ligam, em diagramas entidade-relacionamento por assunto (não um diagrama gigante com tudo).
3. **Regras de negócio:** o que os dados significam, quais valores são válidos, de onde vêm, armadilhas conhecidas. É a parte que ninguém escreve e a que mais falta faz.
4. **Guia de uso:** consultas de exemplo para as perguntas mais comuns e um glossário que liga os termos do negócio às tabelas.

O pg-atlas gera as quatro, em Markdown, com uma camada estruturada (JSON/YAML) por baixo para consumo por máquina.

## Por que Markdown

- É lido diretamente por qualquer LLM, sem conversão.
- Renderiza no GitHub, no Gitea, no Obsidian e em geradores de site estático.
- Funciona bem com git: cada mudança aparece num diff legível.
- Diagramas em Mermaid ficam dentro do próprio arquivo e renderizam no GitHub.

## Organização das pastas (no repositório de documentação)

```
llms.txt                     índice para LLMs (uma linha por schema + links essenciais)
llms-full.txt                versão concatenada para colar inteira num contexto
glossario.md                 termo de negócio -> tabelas/colunas
duvidas.md                   o que a validação não resolveu
catalogo/
  _banco.json                resumo do banco
  _triagem.json              classificação das tabelas
  estrutura/<schema>.json    estrutura completa (determinística)
  estatisticas/<schema>.json estatísticas voláteis
relacionamentos/
  grafo.json                 arestas com confiança e evidência
  lotes.json                 agrupamento usado na documentação
schemas/<schema>/
  README.md                  visão geral do schema
  tabelas/<tabela>.md        uma ficha por tabela, view ou view materializada
  funcoes/<rotina>.md        uma ficha por function, procedure ou gatilho
  _removidos/                fichas de objetos que deixaram de existir
receitas/<assunto>/<slug>.md consultas validadas
semantico/catalogo.yml       catálogo estruturado para ferramentas de text-to-SQL
relatorios/                  relatórios de fase, de mudanças e de custo
```

Nomes de arquivo: o nome do objeto como está no banco, em minúsculas. Rotinas sobrecarregadas (mesmo nome, argumentos diferentes) recebem sufixo `__<n>` e a assinatura completa no título.

## Graus de confiança

Toda afirmação de significado recebe um destes graus:

| Grau | Quando usar |
|---|---|
| `declarado` | Está no catálogo do banco: FK declarada, comentário do banco, tipo, constraint |
| `confirmado` | Foi verificado por consulta (taxa de casamento, contagem) ou está em `config/fatos.yml` |
| `hipotese` | Inferido de nomes, código ou contexto, sem verificação |

No texto, o grau aparece entre colchetes no fim da frase ou na coluna da tabela: `[confirmado]`.

## Marcadores de seção automática

Seções geradas mecanicamente ficam entre:

```
<!-- pg-atlas:auto inicio secao="colunas" -->
...
<!-- pg-atlas:auto fim -->
```

A regeneração reescreve só o que está entre esses marcadores. O texto fora deles (escrito pelos agentes ou por pessoas) é preservado.

## Modelos

Os modelos de cada tipo de arquivo estão em `pg-atlas/templates/`:

- `ficha-tabela.md`
- `ficha-rotina.md`
- `readme-schema.md`
- `receita.md`
- `llms.txt`

## Regras de escrita para os agentes

- Português, frases curtas, sem adjetivos vazios.
- Descrever o que a coluna **significa**, não repetir o nome. Ruim: "id_logradouro: id do logradouro". Bom: "Logradouro do imóvel; liga a `logradouro.id`. Nulo em 3% das linhas [confirmado]".
- Registrar armadilhas explicitamente: formatos inesperados, colunas texto que guardam número, campos livres sem domínio, tabelas multi-tenant, tabelas vazias que parecem importantes.
- Nunca incluir valor real de dado, exceto valores de tabelas de domínio aprovadas e valores de enum do catálogo. Formatos se descrevem pelo padrão ("14 dígitos numéricos, sem máscara"), não por exemplo real.
- Quando não souber, escrever "Significado não determinado" e abrir item em `duvidas.md`, em vez de inventar.
