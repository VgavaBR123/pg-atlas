# pg-atlas

Ferramenta para mapear e documentar bancos PostgreSQL grandes e sem documentação, usando Claude Code com subagentes. O resultado é uma documentação legível por pessoas e por LLMs (Claude, ChatGPT ou modelos locais), pensada para que qualquer pessoa com acesso ao banco consiga pedir a uma IA a query certa.

> Status: planejamento concluído. O código é construído pelo próprio Claude Code a partir do plano, começando pelo [prompt inicial](PROMPT-INICIAL.md).

## Documentos

| Documento | Conteúdo |
|---|---|
| [PROMPT-INICIAL.md](PROMPT-INICIAL.md) | Como montar a pasta de trabalho e o prompt que inicia tudo |
| [docs/PLANO.md](docs/PLANO.md) | O que construir em cada fase, com critérios de aceite |
| [docs/AUTONOMIA.md](docs/AUTONOMIA.md) | Loop autônomo, agentes, política de decisões e gates humanos |
| [docs/FORMATO-DOCUMENTACAO.md](docs/FORMATO-DOCUMENTACAO.md) | Como a documentação do banco é organizada e escrita |
| [docs/CUSTOS.md](docs/CUSTOS.md) | Como o custo é medido e reportado |
| [templates/](templates/) | Modelos de ficha, receita, README de schema, llms.txt e relatório de custos |
| [workspace/](workspace/) | Arquivos copiados para a pasta de trabalho: CLAUDE.md, agentes, permissões, .env.example |

## O problema

Bancos de ERPs e sistemas legados costumam ter milhares de tabelas, poucas FKs declaradas, regras de negócio escondidas em functions e nenhuma documentação. Ferramentas de dicionário de dados resolvem a estrutura, mas não explicam o significado. LLMs conseguem explicar, mas erram quando recebem o schema cru e, se conectadas direto ao banco em paralelo, sobrecarregam a conexão.

## Princípios

- **Somente leitura.** Funciona com um usuário que tem apenas SELECT. Nunca escreve no banco documentado, nem `COMMENT ON`.
- **Uma conexão só.** Os subagentes nunca se conectam ao banco. Toda leitura passa por um único processo, com sessão read-only e timeout.
- **Nenhum dado pessoal.** Nenhuma linha de dado chega ao LLM. O modelo vê apenas estrutura, código de functions e estatísticas agregadas (contagem de linhas, percentual de nulos, cardinalidade). Valores de tabelas de domínio só entram por uma lista explícita aprovada pelo dono do banco.
- **Validação por execução.** Toda afirmação sobre relacionamentos e toda query de exemplo é confirmada por uma consulta de contagem. O que não se confirma é marcado como hipótese.
- **Retomável.** O trabalho é dividido em lotes com estado registrado em disco. Se o job cair, ele continua de onde parou.
- **Incremental.** Uma rodada periódica compara o catálogo novo com o anterior e só redocumenta o que mudou.
- **Agnóstico de banco específico.** Particularidades de cada sistema (filtros de tenant, convenções de nomes, fatos conhecidos) ficam num arquivo de configuração do projeto de documentação, não aqui.

## Fases

| Fase | O que faz | Usa LLM |
|---|---|---|
| 1. Catálogo | Extrai schemas, tabelas, colunas, tipos, índices, views, functions, procedures e triggers | Não |
| 2. Relacionamentos | FKs declaradas, vínculos inferidos por nome e por código, confirmados por contagem | Parcial |
| 3. Tabelas | Ficha de cada tabela: propósito, colunas, domínio, armadilhas | Sim |
| 4. Functions e procedures | O que cada uma faz, o que lê e escreve, quem chama quem | Sim |
| 5. Receitas | Queries de exemplo validadas por execução | Sim |
| 6. Atualização | Diff do catálogo e redocumentação só do que mudou | Parcial |
| 7. Obsidian | Vault enxuto com só o essencial e poucas ligações | Sim |
| 8. Entrega | Relatório final e relatório de custo | Pouco |

## Validação

1. **Mecânica:** toda tabela e coluna citada na documentação precisa existir no catálogo.
2. **Por execução:** relacionamentos e receitas são confirmados por consultas de contagem.
3. **Revisão independente:** um agente que não produziu a ficha revisa contra o catálogo.

O que nenhuma das três resolve vai para uma lista curta de dúvidas para revisão humana.

## Formato de saída

- Uma ficha Markdown por tabela, view e function.
- Um README por schema e um índice raiz no estilo `llms.txt`.
- Um catálogo estruturado em YAML/JSON para consumo por máquina.
- Uma pasta de receitas de consulta validadas.
- Grau de confiança em cada afirmação: `declarado`, `confirmado` ou `hipótese`.

## Ferramentas avaliadas

- [tbls](https://github.com/k1LoW/tbls): extração do catálogo, diff e cobertura de documentação.
- [pglast](https://github.com/lelit/pglast): parser de SQL e PL/pgSQL para extrair dependências das functions sem LLM.
- [SchemaSpy](https://github.com/schemaspy/schemaspy): segunda opinião sobre relacionamentos implícitos.
- [Azimutt](https://github.com/azimuttapp/azimutt): navegação visual em schemas grandes.
- [pgai Semantic Catalog](https://github.com/timescale/pgai) e [WrenAI](https://github.com/Canner/WrenAI): possíveis camadas de consulta em linguagem natural sobre a documentação gerada.

## Licença

A definir.
