# Medição de custo

O dono do projeto precisa apresentar o custo da implementação. Este documento define como o custo é medido durante o trabalho e como o relatório final é montado.

## O que existe para medir

O Claude Code grava cada sessão em arquivos `.jsonl` na pasta de projetos do usuário (no Windows, `%USERPROFILE%\.claude\projects\<pasta-do-workspace-codificada>\`), incluindo as conversas dos subagentes. Cada resposta do modelo traz o modelo usado e o consumo de tokens: entrada, saída, escrita em cache e leitura de cache.

O Claude Code que implementar a fase 0 deve abrir um desses arquivos e confirmar os nomes exatos dos campos antes de escrever o coletor, porque o formato pode mudar entre versões.

## Como a medição funciona

1. **Marcas de fase:** o loop e o orquestrador registram em `atlas-eldados/_controle/custos/fases.jsonl` uma linha no início e outra no fim de cada fase e de cada tarefa:
   `{"evento": "inicio", "fase": 1, "tarefa": "1.2", "quando": "2026-10-23T08:00:00-04:00"}`
2. **Coleta:** `python -m pg_atlas custos coletar` lê os `.jsonl` do Claude Code da pasta do workspace, soma tokens por modelo e distribui por fase e tarefa conforme as marcas de horário. Grava o resultado em `_controle/custos/consumo.json`. Não copia o conteúdo das conversas, só as contagens.
3. **Preços:** `config/precos.yml` (no repositório de documentação) guarda o preço por milhão de tokens de cada modelo, com a fonte e a data da consulta. O Claude Code preenche esse arquivo consultando a página oficial de preços da Anthropic no momento de gerar o relatório. Os preços não ficam embutidos no código.
4. **Câmbio:** a cotação do dólar usada no relatório é a PTAX de venda do Banco Central do dia da geração, com a fonte registrada.

## Duas formas de custo

O relatório separa duas coisas que não são a mesma:

| Custo | O que é | Fonte |
|---|---|---|
| **Desembolso real** | O que de fato foi pago. Com assinatura do Claude, é a mensalidade do plano no período. Com a API, é o gasto mostrado no Console da Anthropic | Valor do plano informado em `config/projeto.yml`; fatura da API, se houver |
| **Custo equivalente em API** | Quanto o mesmo consumo de tokens custaria se fosse pago pela API, com os preços públicos | Tokens coletados × `precos.yml` |

O custo equivalente é útil para comparar, orçar o futuro e mostrar o valor do trabalho, mas não é o que saiu do caixa. O relatório deixa isso explícito.

## Relatório

`python -m pg_atlas custos relatorio` gera `relatorios/custo-implementacao.md`, seguindo o modelo `templates/relatorio-custos.md`, com:

- Período, número de sessões e horas de execução.
- Tokens por modelo e por tipo (entrada, saída, cache).
- Custo equivalente em API, em dólar e em real, por fase e no total.
- Desembolso real no período.
- Produção: objetos documentados, receitas validadas.
- Indicadores: custo por objeto documentado e custo estimado de cada atualização mensal (com base no consumo da fase 6).
- Premissas e limitações da medição.

O relatório também é gerado parcialmente ao fim de cada fase (`relatorios/custo-parcial-fase-<n>.md`), para acompanhamento.

## Comandos e formatos

Todos recebem `--doc <pasta>`.

- `custos marcar-inicio|marcar-fim --fase N [--tarefa X]`: acrescenta uma linha a `_controle/custos/fases.jsonl` (a chave `tarefa` só existe quando informada; linhas anteriores nunca são alteradas).
- `custos coletar [--projetos <pasta>]`: lê todos os `.jsonl` da pasta (padrão `%USERPROFILE%\.claude\projects\<workspace codificado>`, subpastas `<sessao>/subagents` incluídas) e grava `_controle/custos/consumo.json`. A pasta é o caminho do workspace com todo caractere que não é letra ou dígito trocado por `-` (`C:\dev\atlas` vira `C--dev-atlas`).
- `custos relatorio [--parcial --fase N]`: gera `relatorios/custo-implementacao.md` ou `relatorios/custo-parcial-fase-<n>.md`. Exige o `consumo.json` (rode `coletar` antes).

Regras da coleta:

- Só linhas com `type == "assistant"`; linhas malformadas e modelos sintéticos (`<synthetic>`) são ignorados.
- A mesma resposta aparece em várias linhas (uma por bloco de conteúdo, com o mesmo `usage`). Deduplica-se por `message.id`, ou por `requestId` se faltar: valem os tokens da última ocorrência e o instante da primeira.
- Atribuição por horário: a tarefa aberta mais interna (início mais recente) vence; senão a fase aberta; senão `sem_fase`. Um início sem fim fica aberto até o fim dos dados. O fim vale até o fim do segundo marcado.
- Horas de execução: soma, por sessão, do intervalo entre a primeira e a última mensagem do assistente. É uma aproximação por cima: pausas dentro da sessão contam. As conversas de subagentes entram na sessão que as originou. As horas por fase e por tarefa usam a mesma conta sobre as mensagens do balde.
- Só contagens são gravadas: nenhum texto de conversa e nenhum caminho de log.

### `config/precos.yml`

Preço em dólar por milhão de tokens, por modelo. O nome do modelo é o dos logs; se não houver chave exata vale a maior chave que seja prefixo do nome (sufixos de data).

```yaml
modelos:
  claude-exemplo-1:
    entrada: 0.0
    saida: 0.0
    cache_escrita_5m: 0.0
    cache_escrita_1h: 0.0
    cache_leitura: 0.0
    fonte: "https://..."        # página oficial de preços
    consultado_em: "AAAA-MM-DD"
```

### `config/cambio.yml`

```yaml
ptax_venda: 0.0               # reais por dólar
fonte: "Banco Central do Brasil, PTAX"
data: "AAAA-MM-DD"
```

Se faltar o preço de um modelo (ou de algum tipo de token com consumo) ou o câmbio, o relatório sai assim mesmo, com "preço não informado" ou "câmbio não informado" e um aviso nas premissas. O desembolso real vem de `custos.plano` e `custos.valor_mensal_brl` em `config/projeto.yml`; vazios, aparece "não informado". A produção é a contagem de `schemas/*/tabelas/*.md`, `schemas/*/funcoes/*.md` e `receitas/**/*.md` (as receitas ainda não distinguem as validadas).
