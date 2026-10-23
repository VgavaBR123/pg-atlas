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
  _fichas.json               mapa objeto -> caminho da ficha
  estrutura/<schema>.json    estrutura completa (determinística)
  estatisticas/<schema>.json estatísticas voláteis
relacionamentos/
  grafo.json                 arestas com confiança e evidência
  lotes.json                 agrupamento usado na documentação
schemas/<schema>/
  README.md                  visão geral do schema
  tabelas/<tabela>.md        uma ficha por tabela, view ou view materializada
  funcoes/<rotina>.md        uma ficha por function, procedure, agregado ou função de janela (inclui a função de gatilho)
  _removidos/                fichas de objetos que deixaram de existir
receitas/<assunto>/<slug>.md consultas validadas
semantico/catalogo.yml       catálogo estruturado para ferramentas de text-to-SQL
obsidian/                    vault enxuto com só o essencial (fase 7)
relatorios/                  relatórios de fase, de mudanças e de custo
```

Nomes de arquivo, aplicados nesta ordem:

1. Marcador de mascaramento `[mascarado:<tipo>:<N>]` vira `mascarado-<tipo>-<N>`.
2. Minúsculas.
3. Só letras e dígitos Unicode, `_`, `-` e `$`. Qualquer outro caractere (inclusive `.`, espaço, `~`) vira `~XX` por byte UTF-8, em hexadecimal maiúsculo.
4. Nome reservado do Windows (`con`, `prn`, `aux`, `nul`, `com0`-`com9`, `lpt0`-`lpt9`) recebe `~` no fim.
5. Nome com mais de 120 caracteres vira os primeiros 100 + `~` + 8 caracteres do SHA-1 do nome original.
6. Objetos da mesma pasta que acabam com o mesmo nome (rotinas sobrecarregadas ou nomes que só diferem na caixa) recebem todos o sufixo `__<n>`, na ordem do nome original e dos argumentos de identidade. Objeto sem colisão não leva sufixo. A assinatura completa fica no título.

Sobrando colisão (sem diferenciar caixa), o comando falha sem gerar nada. O mapa objeto -> caminho fica em `catalogo/_fichas.json`.

Gatilhos não têm ficha própria: aparecem na ficha da tabela e na ficha da função de gatilho.

## Graus de confiança

Toda afirmação de significado recebe um destes graus:

| Grau | Quando usar |
|---|---|
| `declarado` | Está no catálogo do banco: FK declarada, comentário do banco, tipo, constraint |
| `confirmado` | Foi verificado por consulta (taxa de casamento, contagem) ou está em `config/fatos.yml` |
| `hipotese` | Inferido de nomes, código ou contexto, sem verificação |

No texto, o grau aparece entre colchetes no fim da frase ou na coluna da tabela: `[confirmado]`.

## Marcadores de seção automática

Seções geradas mecanicamente ficam entre marcadores, cada um sozinho na sua linha:

```
<!-- pg-atlas:auto inicio secao="colunas" -->
...
<!-- pg-atlas:auto fim -->
```

O nome da seção usa só `[a-z0-9_]` e não se repete no mesmo arquivo. Seções não se aninham.

A regeneração reescreve só o que está entre esses marcadores. O texto fora deles (escrito pelos agentes ou por pessoas) é preservado. Seção automática ausente é reinserida na posição canônica. Marcadores quebrados (início sem fim, fim sem início, aninhamento, nome repetido ou ausente) fazem o arquivo ficar intacto e o comando terminar com erro.

Seções de significado, que os agentes preenchem, ficam fora dos marcadores. Enquanto estão vazias, trazem logo abaixo do título:

```
<!-- pg-atlas:pendente fase="<n>" -->
_Pendente: a preencher na fase <n>._
```

Quem preenche a seção apaga essas duas linhas. A contagem de pendências é feita pelo comentário. Isso não se confunde com "Significado não determinado", que é resultado do agente.

Seções automáticas esperadas, na ordem canônica:

- Ficha de tabela: `cabecalho`, `colunas`, `relacionamentos`, `indices`, `rotinas`, `dependencias`, e conforme o caso `definicao` (view) e `particoes` (tabela particionada). Partição tem ficha resumida: `cabecalho`, `indices`, `rotinas`.
- Ficha de rotina: `cabecalho`, `dependencias`, `codigo`.
- README do schema: `cabecalho`, `inventario`, `rotinas`, `sequencias`, `tipos`.
- `llms.txt`: `llms`.

Seção esperada sem conteúdo traz "Nenhum." e não é omitida.

## Modelos

Os modelos de cada tipo de arquivo estão em `pg-atlas/templates/`:

- `ficha-tabela.md`
- `ficha-rotina.md`
- `readme-schema.md`
- `receita.md`
- `llms.txt`
- `nota-obsidian.md`

## Regras de escrita para os agentes

- Português, frases curtas, sem adjetivos vazios.
- Descrever o que a coluna **significa**, não repetir o nome. Ruim: "id_logradouro: id do logradouro". Bom: "Logradouro do imóvel; liga a `logradouro.id`. Nulo em 3% das linhas [confirmado]".
- Registrar armadilhas explicitamente: formatos inesperados, colunas texto que guardam número, campos livres sem domínio, tabelas multi-tenant, tabelas vazias que parecem importantes.
- Nunca incluir valor real de dado, exceto valores de tabelas de domínio aprovadas e valores de enum do catálogo. Formatos se descrevem pelo padrão ("14 dígitos numéricos, sem máscara"), não por exemplo real.
- Quando não souber, escrever "Significado não determinado" e abrir item em `duvidas.md`, em vez de inventar.
