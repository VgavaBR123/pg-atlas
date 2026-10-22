# Plano de implementação do pg-atlas

Este documento diz o que o Claude Code precisa construir em cada fase, o que cada fase entrega e como saber que ela terminou. É a fonte de verdade do projeto: o loop autônomo (ver `AUTONOMIA.md`) lê este arquivo para decidir a próxima tarefa.

## Visão geral

O trabalho acontece numa pasta de trabalho (workspace) que contém os dois repositórios lado a lado:

```
<workspace>/                 # pasta criada pelo dono do projeto, fora de qualquer repositório
├── CLAUDE.md                # copiado de pg-atlas/workspace/ na fase 0
├── .claude/                 # agentes e permissões, copiados de pg-atlas/workspace/.claude/
├── .venv/                   # ambiente Python, criado na fase 0
├── pg-atlas/                # a ferramenta (código genérico, sem nada do banco documentado)
└── atlas-eldados/           # a documentação do banco (específico, privado)
```

Nos exemplos deste documento, o repositório de documentação é o `atlas-eldados`. Regra de separação, verificada pelo agente `guardiao` em todo commit:

- **pg-atlas** recebe somente código, templates e documentação genéricos. Nenhum nome de schema, tabela, UUID, host ou regra de negócio do banco documentado.
- **atlas-eldados** recebe a configuração do banco (`config/`), a saída gerada (catálogo, fichas, receitas), o controle do trabalho (`_controle/`) e os relatórios (`relatorios/`).

## Arquitetura em uma frase

Um único processo Python (`pg_atlas`) é o único que fala com o banco, por uma conexão read-only. Tudo o que ele lê vira arquivo. Os agentes do Claude só leem e escrevem arquivos e, quando precisam confirmar algo no banco, pedem ao `pg_atlas` para executar uma consulta de verificação.

```
            banco (somente leitura, 1 conexão)
                       │
                 pg_atlas (Python)
     extrai catálogo · executa verificações · aplica guardas
                       │
                 arquivos JSON/MD
                       │
   orquestrador (Claude Code) ── subagentes (documentador, revisor, ...)
                       │
              atlas-eldados (git)
```

## Regras que valem para todas as fases

1. **Somente leitura.** O módulo de conexão abre a sessão com `default_transaction_read_only = on`, `statement_timeout` configurável (padrão 60 s), `lock_timeout` de 5 s e `application_name = pg-atlas`. Antes de executar qualquer SQL, um guarda rejeita o que não começar por `SELECT`, `WITH`, `EXPLAIN` ou `SHOW`, e rejeita qualquer comando com `;` intermediário.
2. **Uma conexão.** Um arquivo de trava (`_controle/db.lock`) garante um único processo conectado. Erro "too many connections" gera espera com backoff (30 s, 60 s, 120 s, depois aborta a tarefa e registra).
3. **Credenciais fora do repositório.** A conexão lê o arquivo `.env` da pasta de trabalho (fora dos dois repositórios), com `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER` e `PGPASSWORD`. Como alternativa, aceita `PGSERVICE` com `pg_service.conf` e `pgpass.conf`. Nenhuma senha, host ou usuário em arquivo versionado. Os dois repositórios ignoram `.env` no `.gitignore`, e o guardião faz busca por padrões de credencial antes de cada commit. Nenhum agente lê o `.env`: só o módulo de conexão.
4. **Nenhum dado pessoal.** Nunca se lê conteúdo de tabela, exceto (a) contagens e agregados numéricos, (b) tabelas que constem em `atlas-eldados/config/dominios.yml` (lista aprovada pelo dono). Do `pg_stats` só se leem `null_frac`, `n_distinct`, `avg_width` e `correlation`. As colunas `most_common_vals`, `most_common_freqs`, `histogram_bounds`, `most_common_elems` e semelhantes são proibidas: o guarda rejeita SQL que as mencione.
5. **Sem carga pesada.** Nada de `count(*)` em tabela grande: contagem vem de `pg_class.reltuples`. Verificações de relacionamento usam amostragem (`TABLESAMPLE SYSTEM`) com `LIMIT`. Execuções longas preferencialmente fora do expediente (configurável em `config/projeto.yml`).
6. **Saída determinística.** JSON com chaves e listas ordenadas, para que o `git diff` mostre só mudanças reais. Estatísticas voláteis (linhas, tamanho, último analyze) ficam em arquivos separados da estrutura.
7. **Retomável.** Toda fase grava progresso por item em `atlas-eldados/_controle/estado.json`. Uma fase interrompida recomeça do item seguinte ao último concluído.
8. **Medição de custo.** Toda fase registra início e fim em `_controle/custos/fases.jsonl` (ver `CUSTOS.md`).
9. **Commits pequenos.** Um commit por tarefa concluída e validada, mensagem em português, no repositório certo.

## Fases

| Fase | Nome | Usa LLM para gerar conteúdo | Depende de banco |
|---|---|---|---|
| 0 | Fundação | Não | Só no teste de conexão |
| 1 | Catálogo completo | Não | Sim |
| 2 | Relacionamentos | Pouco | Sim (verificação) |
| 3 | Fichas das tabelas | Sim | Sim (verificação) |
| 4 | Functions, procedures e triggers | Sim | Não |
| 5 | Receitas, índice e glossário | Sim | Sim (verificação) |
| 6 | Atualização incremental | Parcial | Sim |
| 7 | Relatório final e entrega | Pouco | Não |

---

## Fase 0 — Fundação

**Objetivo:** preparar o workspace, o ambiente e as peças que todas as outras fases usam. Nenhum conteúdo de documentação é gerado aqui.

### O que construir

| # | Item | Onde | Detalhe |
|---|---|---|---|
| 0.1 | Preparar workspace | workspace | Rodar `pg-atlas/scripts/preparar-workspace.ps1`: copia `CLAUDE.md` e `.claude/` para a pasta-mãe. Se já existirem, só atualiza. |
| 0.2 | Ambiente Python | workspace | `.venv` com Python 3.11+. Dependências em `pg-atlas/pyproject.toml`: `psycopg[binary]` (v3), `python-dotenv`, `pyyaml`, `jsonschema`, `sqlglot`, `pytest`. `pglast` é opcional (pode não instalar no Windows; ver fase 4). |
| 0.3 | Pacote `pg_atlas` | pg-atlas | Estrutura `src/pg_atlas/` com CLI `python -m pg_atlas <comando>`. |
| 0.4 | Módulo de conexão | pg-atlas | `conexao.py`: carrega o `.env` da pasta de trabalho (ou usa `PGSERVICE`), aplica as configurações de sessão da regra 1, trava de processo único (regra 2), backoff, guarda de SQL (regras 1 e 4). Nunca imprime nem grava em log os valores de conexão. |
| 0.5 | Configuração do projeto | atlas-eldados | Ler `config/projeto.yml` (nome do serviço, schemas incluídos, horário permitido, timeouts). O `pg_atlas` recebe o caminho do repositório de documentação por argumento `--doc <pasta>`. |
| 0.6 | Comando `diagnostico` | pg-atlas | Conecta, mostra versão do PostgreSQL, usuário, se a sessão é read-only, número de schemas visíveis, limite de conexões da role (`rolconnlimit`) e conexões em uso. Não lê dado de tabela. |
| 0.7 | Testes offline | pg-atlas | `pytest` sem banco: guarda de SQL (aceita/rejeita), leitura de config, formatação determinística de JSON. |
| 0.8 | Coletor de custos | pg-atlas | `custos.py` com os comandos `custos marcar-inicio`, `custos marcar-fim` e `custos relatorio` (ver `CUSTOS.md`). |
| 0.9 | Loop autônomo | pg-atlas | `tools/loop.py` (ver `AUTONOMIA.md`). |
| 0.10 | Controle | atlas-eldados | Criar `_controle/ESTADO.md`, `_controle/DECISOES.md`, `_controle/PENDENCIAS-HUMANAS.md`, `_controle/estado.json`, `_controle/custos/`. Adicionar `_controle/db.lock` ao `.gitignore`. |
| 0.11 | Verificar credencial | workspace | Conferir se existe `.env` na pasta de trabalho com as cinco variáveis preenchidas (sem ler nem exibir os valores; só testar se estão vazias). Se não, registrar a pendência G1 e seguir com os itens que não precisam de banco. |
| 0.12 | Verificar visibilidade | atlas-eldados | Com `gh api repos/<dono>/<repo> --jq .visibility`, confirmar que o repositório é privado. Se não der para confirmar, commitar sem fazer push e registrar a pendência G2. |

### Critérios de aceite

- `pytest` passa.
- `python -m pg_atlas diagnostico --doc ../atlas-eldados` mostra a sessão como read-only (ou, se G1 estiver aberta, falha com mensagem clara apontando o arquivo a preencher).
- Uma tentativa de executar `UPDATE`, `DELETE`, `INSERT`, `CREATE`, `DROP`, `ALTER`, `TRUNCATE`, `GRANT`, `COPY`, `CALL`, `DO` ou SQL com `most_common_vals` é rejeitada pelo guarda antes de chegar ao banco (teste automatizado).
- Nenhum arquivo versionado contém senha, host ou usuário (verificado pelo guardião).

---

## Fase 1 — Catálogo completo

**Objetivo:** extrair a estrutura inteira do banco para arquivos, sem LLM, e já gerar um esqueleto de documentação navegável. Ao fim desta fase existe uma ficha mecânica para cada objeto do banco.

### O que construir

#### 1.1 Consultas de catálogo

Arquivos `.sql` em `pg-atlas/src/pg_atlas/sql/catalogo/`, um por tipo de objeto, todos filtrando por schema (parâmetro) e excluindo `pg_catalog`, `information_schema`, `pg_toast` e schemas temporários.

| Arquivo | Fonte | O que extrai |
|---|---|---|
| `schemas.sql` | `pg_namespace` | nome, dono, comentário |
| `relacoes.sql` | `pg_class` | tabelas (r), particionadas (p), views (v), materializadas (m), estrangeiras (f); dono; comentário; se é partição e de quem |
| `colunas.sql` | `pg_attribute`, `pg_attrdef`, `col_description` | ordem, nome, tipo (`format_type`), nulidade, default, identidade, gerada, comentário |
| `restricoes.sql` | `pg_constraint` | PK, UK, FK (com colunas dos dois lados, `ON DELETE/UPDATE`), CHECK (`pg_get_constraintdef`) |
| `indices.sql` | `pg_index` | nome, colunas, único, parcial, definição (`pg_get_indexdef`) |
| `views.sql` | `pg_views`, `pg_matviews` | definição (`pg_get_viewdef`) |
| `funcoes.sql` | `pg_proc` | nome, tipo (function/procedure/aggregate/window), linguagem, argumentos, retorno, volatilidade, `SECURITY DEFINER`, código (`pg_get_functiondef`, exceto agregados) |
| `gatilhos.sql` | `pg_trigger` | gatilhos não internos, tabela, evento, momento, função chamada (`pg_get_triggerdef`) |
| `sequencias.sql` | `pg_sequence` | nome, tipo, dono (coluna) |
| `tipos.sql` | `pg_type`, `pg_enum` | tipos compostos, domínios e enums (valores do enum são metadado, não dado pessoal) |
| `dependencias.sql` | `pg_depend`, `pg_rewrite` | de quais tabelas cada view depende |
| `privilegios.sql` | `has_table_privilege`, `has_function_privilege` | se a credencial atual tem SELECT/EXECUTE em cada objeto |
| `estatisticas.sql` | `pg_class`, `pg_stat_user_tables`, `pg_total_relation_size` | linhas estimadas, tamanho, último analyze, scans sequenciais e por índice (indica tabelas realmente usadas) |
| `estatisticas_colunas.sql` | `pg_stats` | **somente** `null_frac`, `n_distinct`, `avg_width`, `correlation` |

Objetos sem permissão de SELECT entram no catálogo com a estrutura (o catálogo do PostgreSQL mostra) e a marca `acesso: sem_select`.

#### 1.2 Extrator

Comando `python -m pg_atlas catalogo --doc ../atlas-eldados [--schema X] [--retomar]`.

- Processa um schema por vez, na ordem alfabética, gravando o progresso em `_controle/estado.json`.
- Saída estrutural: `atlas-eldados/catalogo/estrutura/<schema>.json`.
- Saída volátil: `atlas-eldados/catalogo/estatisticas/<schema>.json`.
- Resumo do banco: `atlas-eldados/catalogo/_banco.json` (versão, tamanho total, contagem por tipo de objeto e por schema, data da extração).
- O formato dos arquivos é definido por um JSON Schema em `pg-atlas/src/pg_atlas/esquemas/catalogo.schema.json` e validado ao gravar.

#### 1.3 Triagem

Comando `python -m pg_atlas triagem --doc ../atlas-eldados`. Classifica cada tabela, sem LLM, para orientar as fases seguintes. Saída em `catalogo/_triagem.json`.

| Classe | Regra |
|---|---|
| `vazia` | `reltuples` igual a 0 e `n_live_tup` igual a 0 |
| `temporaria_ou_copia` | nome com sufixo ou prefixo típico (`_temp`, `_tmp`, `_bkp`, `_backup`, `_old`, `_copia`, sufixo de data, prefixo `_`) |
| `dominio_candidata` | até 500 linhas estimadas e nome com `tipo`, `situacao`, `status`, `categoria`, `classe`, `natureza`, `motivo`, `configuracao` |
| `fato_grande` | mais de 1 milhão de linhas estimadas |
| `comum` | o resto |

As candidatas a domínio são gravadas em `config/dominios-candidatos.yml` para o dono aprovar. A lista aprovada é `config/dominios.yml`, que começa vazia.

#### 1.4 Esqueleto da documentação

Comando `python -m pg_atlas esqueleto --doc ../atlas-eldados`. A partir do catálogo, gera em Markdown, seguindo `FORMATO-DOCUMENTACAO.md`:

- `schemas/<schema>/README.md` com a lista de tabelas, views e functions do schema, contagens e triagem.
- `schemas/<schema>/tabelas/<tabela>.md` com as seções mecânicas preenchidas (colunas, chaves, índices, gatilhos, estatísticas) e as seções de significado marcadas como pendentes.
- `schemas/<schema>/funcoes/<funcao>.md` com assinatura, linguagem e código.
- `llms.txt` provisório na raiz, apontando para os READMEs dos schemas.

As seções mecânicas ficam entre marcadores `<!-- pg-atlas:auto -->` e `<!-- /pg-atlas:auto -->`. Regenerar o esqueleto só reescreve o que está entre marcadores e nunca apaga o texto que os agentes escreveram nas fases seguintes.

#### 1.5 Verificação da fase

Comando `python -m pg_atlas verificar catalogo --doc ../atlas-eldados`:

- Para cada schema, compara o número de objetos no JSON com uma contagem direta no `pg_class`/`pg_proc` (consulta leve, só catálogo).
- Valida todos os JSON contra o JSON Schema.
- Procura no catálogo qualquer chave ou valor proibido (`most_common_vals` e semelhantes). Achou, falha.
- Confere que todo objeto do catálogo tem ficha no esqueleto e vice-versa.

#### 1.6 Relatório da fase

`relatorios/fase-1-catalogo.md`: números do banco (schemas, tabelas, views, functions, procedures, gatilhos, sequências), distribuição da triagem, maiores tabelas, objetos sem acesso, tempo de execução, custo da fase.

### Critérios de aceite da fase 1

- Todos os schemas configurados extraídos, sem erro, com `estado.json` mostrando 100%.
- `verificar catalogo` passa.
- Existe uma ficha por tabela, view e function.
- Rodar a extração duas vezes seguidas sem mudança no banco gera `git diff` vazio na pasta `catalogo/estrutura/`.
- Nenhuma linha de dado de tabela foi lida (o guarda registra em log todo SQL executado; o validador confere que nenhum `FROM` aponta para tabela de usuário fora da lista de domínio).

---

## Fase 2 — Relacionamentos

**Objetivo:** montar o grafo de como as tabelas se ligam, mesmo onde o banco não declara FK, e dividir o banco em grupos de tabelas relacionadas para as fases 3 e 4.

### O que construir

1. **FKs declaradas:** vêm direto do catálogo, confiança `declarado`.
2. **Inferência por nome** (sem LLM): coluna `id_<x>` ou `<x>_id` com tipo igual ao da PK de uma tabela cujo nome termina em `<x>` (com e sem prefixos de módulo). Gera candidatos com pontuação.
3. **Inferência por código** (sem LLM): joins encontrados nas definições de views (parser `sqlglot`) e nos corpos das functions (`pglast` quando disponível, senão extração dos comandos SQL do corpo e `sqlglot`).
4. **Verificação por amostragem:** para cada candidato, o `pg_atlas` sorteia até 1.000 valores não nulos da coluna filha (`TABLESAMPLE SYSTEM`, com `statement_timeout`) e conta quantos existem na tabela pai. Grava só a taxa de casamento, nunca os valores. Taxa de 95% ou mais vira `confirmado`. Entre 50% e 95% vira `hipotese` com nota. Abaixo disso o candidato é descartado. Tabelas sem SELECT ficam como `hipotese` sem verificação.
5. **Filtro de tenant:** se `config/fatos.yml` declarar coluna de tenant, a verificação aplica o filtro, para não misturar clientes.
6. **Agente `analista-relacionamentos`:** revisa os candidatos ambíguos (mais de uma tabela pai possível) olhando nomes, comentários e código, e escolhe ou marca como dúvida.
7. **Agrupamento:** componentes conexos e detecção de comunidades no grafo, limitados por tamanho, para formar os lotes de documentação.

### Saídas

- `relacionamentos/grafo.json`: arestas com origem, destino, colunas, confiança, taxa de casamento, evidência.
- `relacionamentos/lotes.json`: lotes para a fase 3, com as tabelas de cada lote.
- `schemas/<schema>/README.md`: seção de relacionamentos com diagrama Mermaid dos grupos principais.
- `relatorios/fase-2-relacionamentos.md`.

### Critérios de aceite

- Toda FK declarada está no grafo.
- Toda aresta `confirmado` tem taxa de casamento registrada.
- Nenhuma tabela `comum` ou `fato_grande` fica fora de algum lote.
- Nenhum valor de dado foi gravado, só taxas.

---

## Fase 3 — Fichas das tabelas

**Objetivo:** explicar o significado de cada tabela e coluna, com grau de confiança.

### O que construir

1. **Pacote de contexto por lote** (sem LLM): para cada lote da fase 2, um arquivo `_controle/lotes/<id>.json` com a estrutura das tabelas, estatísticas de coluna permitidas, relacionamentos, views e functions que as usam, fatos conhecidos (`config/fatos.yml`) e valores das tabelas de domínio aprovadas.
2. **Ordem:** lotes das tabelas `vazia` e `temporaria_ou_copia` recebem só uma linha de descrição e uma etiqueta, em tratamento leve. Os lotes restantes seguem por schema.
3. **Documentação:** o orquestrador dispara o agente `documentador`, um por lote, vários em paralelo (padrão 4). Ele preenche as seções de significado das fichas (propósito, descrição das colunas, granularidade, armadilhas, consultas típicas) e marca cada afirmação com a confiança.
4. **Hipóteses verificáveis:** quando o documentador quer confirmar algo, escreve a pergunta como SQL de agregado em `_controle/verificacoes/pendentes/`. O `pg_atlas verificar fila` executa sob as regras de segurança e devolve o resultado (contagem, taxa) em `_controle/verificacoes/respondidas/`.
5. **Revisão:** o agente `revisor` (que não escreveu o lote) confere a ficha contra o catálogo e o pacote. Depois, o `pg_atlas verificar fichas` faz a checagem mecânica: toda tabela e coluna citada existe.
6. **Reprovação:** volta ao documentador com a lista de problemas, no máximo duas vezes. Depois disso, o item vai para `duvidas.md` e o lote é concluído com o que foi aprovado.

### Critérios de aceite

- Toda ficha de tabela `comum` e `fato_grande` tem propósito e descrição de todas as colunas.
- `verificar fichas` passa (zero nomes inexistentes).
- Toda afirmação está marcada com `declarado`, `confirmado` ou `hipotese`.

---

## Fase 4 — Functions, procedures e triggers

**Objetivo:** explicar o que cada rotina faz e como elas se encadeiam.

### O que construir

1. **Grafo de dependências** (sem LLM): para cada rotina, as tabelas lidas, as tabelas escritas (INSERT/UPDATE/DELETE) e as rotinas chamadas. Usa `pglast.parse_plpgsql` quando disponível. No Windows, se o `pglast` não instalar, o fallback é extrair os comandos SQL do corpo e analisá-los com `sqlglot`. SQL dinâmico (`EXECUTE`) é marcado como `dinamico` para leitura pelo agente.
2. **Mapa de gatilhos:** tabela → evento → rotina.
3. **Lotes:** rotinas agrupadas pelo grafo de chamadas, com no máximo cerca de 3.000 linhas de código por lote.
4. **Documentação:** o agente `documentador` (em modo rotina) preenche a ficha com objetivo, parâmetros, retorno, o que lê, o que escreve, regras de negócio, efeitos colaterais e quem chama. O agente `revisor` revisa.
5. **Atualização das fichas de tabela:** seção "Rotinas que leem/escrevem esta tabela".

### Critérios de aceite

- Toda rotina tem ficha com objetivo e lista de tabelas lidas e escritas.
- O grafo de chamadas não referencia rotina inexistente.

---

## Fase 5 — Receitas, índice e glossário

**Objetivo:** tornar a documentação útil para quem quer montar consultas com ajuda de uma LLM.

### O que construir

1. **Banco de perguntas:** perguntas típicas por assunto (os assuntos vêm de `config/projeto.yml`), geradas a partir dos fatos e das fichas, e gravadas em `receitas/_perguntas.yml`.
2. **Receitas:** para cada pergunta, uma receita `receitas/<assunto>/<slug>.md` com a pergunta, o SQL, a explicação dos joins e filtros, e as armadilhas.
3. **Validação das receitas:** cada SQL passa pelo guarda, por `EXPLAIN` e por uma execução envolvida em `SELECT count(*) FROM (<receita>) q` com timeout. Grava-se só se executou, quantas linhas retornou e o tempo. Receita que falha volta para correção, no máximo duas vezes, e depois vai para `duvidas.md`.
4. **Glossário:** `glossario.md`, com termo de negócio → tabelas e colunas (por exemplo, "cliente ativo", "data de competência").
5. **Índice para LLMs:** `llms.txt` final, com uma descrição de uma linha por schema, os links para os READMEs, o glossário, as receitas e os fatos essenciais (filtro de tenant e afins), e `llms-full.txt` com o conteúdo concatenado dos READMEs de schema e do glossário.
6. **Catálogo semântico estruturado:** `semantico/catalogo.yml`, com tabelas, colunas e descrições em formato de máquina, pronto para importar em ferramentas como pgai Semantic Catalog ou WrenAI.

### Critérios de aceite

- Toda receita publicada executou com sucesso.
- O `llms.txt` aponta só para arquivos existentes.

---

## Fase 6 — Atualização incremental

**Objetivo:** manter a documentação em dia quando o fornecedor mudar o banco.

### O que construir

1. Comando `python -m pg_atlas atualizar --doc ../atlas-eldados`: reextrai o catálogo, compara com a versão commitada e gera `relatorios/mudancas-<data>.md` com objetos novos, removidos e alterados (colunas, tipos, código de rotinas).
2. Marca as fichas afetadas como `desatualizada` e coloca na fila só os lotes correspondentes das fases 2 a 5.
3. Objetos removidos têm a ficha movida para `schemas/<schema>/_removidos/` com a data, sem apagar o histórico.
4. Script de agendamento para o Agendador de Tarefas do Windows (mensal por padrão), entregue como instrução em `docs/` do repositório de documentação. O agendamento em si é criado pelo dono.

### Critérios de aceite

- Rodar `atualizar` sem mudança no banco não altera nenhum arquivo.
- Uma mudança simulada (catálogo de teste com uma coluna a mais) gera exatamente uma ficha marcada.

---

## Fase 7 — Relatório final e entrega

1. `python -m pg_atlas custos relatorio` gera `relatorios/custo-implementacao.md` (ver `CUSTOS.md`).
2. `relatorios/relatorio-final.md`: o que foi documentado (números), cobertura (percentual de tabelas, colunas e rotinas com descrição), distribuição de confiança, dúvidas em aberto, como usar, como atualizar e custo.
3. Revisão final pelo `guardiao`: nada específico do banco no pg-atlas, nada sensível no atlas-eldados.
4. Atualizar os READMEs dos dois repositórios com o estado final.
