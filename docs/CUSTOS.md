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
