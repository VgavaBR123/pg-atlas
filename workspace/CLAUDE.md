# Workspace pg-atlas

Esta pasta contém dois repositórios lado a lado:

- `pg-atlas/`: a ferramenta. Código, templates e documentação **genéricos**.
- `atlas-eldados/`: a documentação do banco documentado. **Privado.** Configuração, saída gerada, controle do trabalho e relatórios.

(Se o repositório de documentação tiver outro nome, o script de preparação do workspace substitui `atlas-eldados` neste arquivo.)

O trabalho é autônomo. O dono não responde perguntas durante a execução. Leia, nesta ordem:

1. `pg-atlas/docs/PLANO.md`: o que construir em cada fase e os critérios de aceite.
2. `pg-atlas/docs/AUTONOMIA.md`: os laços de execução, a política de decisões e os gates humanos.
3. `pg-atlas/docs/FORMATO-DOCUMENTACAO.md`: como a documentação é escrita.
4. `pg-atlas/docs/CUSTOS.md`: como o custo é medido.
5. `atlas-eldados/docs/INSUMOS.md` e `atlas-eldados/config/`: dados do banco documentado.
6. `atlas-eldados/_controle/ESTADO.md`: onde o trabalho parou (se já existir).

## Regras invioláveis

Nenhuma decisão, de nenhum agente, passa por cima destas regras:

1. **Somente leitura no banco.** Nenhum comando que altere o banco, nem em schema onde a credencial tenha permissão de escrita. Toda consulta passa pelo `pg_atlas` e pelo guarda de SQL.
2. **Uma conexão.** Só o `pg_atlas` conecta, um processo por vez. Agentes nunca abrem conexão própria, nunca usam `psql` e nunca escrevem código que conecte fora do módulo `conexao.py`.
3. **Credenciais.** O arquivo `.env` desta pasta guarda a conexão. **Nenhum agente lê, exibe, copia ou resume o `.env`.** Nada de host, usuário ou senha em arquivo versionado, log, relatório ou mensagem.
4. **Nenhum dado pessoal.** Nunca ler conteúdo de tabela além de contagens e agregados, salvo as tabelas listadas em `atlas-eldados/config/dominios.yml`. Nunca ler `most_common_vals`, `histogram_bounds` ou semelhantes. Nenhum valor real de dado em ficha, relatório ou commit.
5. **pg-atlas genérico.** Nada específico do banco documentado (nomes de schema, tabela, UUID, regra de negócio) em `pg-atlas/`. O que é específico vai para `atlas-eldados/config/`.
6. **Push só para repositório privado confirmado.** Antes do primeiro push do `atlas-eldados`, confirmar que é privado com `gh api repos/<dono>/atlas-eldados --jq .visibility` (resultado `private`). O dono e o nome do repositório vêm de `git remote get-url origin`. Sem confirmação, commitar localmente e registrar o gate G2.
7. **Nada destrutivo no git:** sem `push --force`, sem `reset --hard` em branch publicada, sem reescrever histórico.

## Como trabalhar

- Siga o laço da tarefa de `AUTONOMIA.md`: implementador → validador → guardião → commit → estado.
- Dúvida que iria para o dono vai para o agente `decisor`. Não pergunte nada ao usuário.
- Ao iniciar e terminar fase ou tarefa, registre a marca no coletor de custos.
- Um commit por tarefa aprovada, no repositório certo, mensagem em português.
- Atualize `atlas-eldados/_controle/ESTADO.md` e `estado.json` ao fim de cada tarefa.
- Use o Python de `.venv` desta pasta.
- Se os agentes personalizados de `.claude/agents/` ainda não estiverem disponíveis nesta sessão (acontece na primeira sessão, logo após a preparação do workspace), use o agente de uso geral passando o conteúdo do arquivo do agente como instrução de papel.

## Agentes

| Agente | Papel |
|---|---|
| `decisor` | Responde no lugar do dono, segundo a política de decisões, e registra em `DECISOES.md` |
| `implementador` | Implementa uma tarefa de código do plano, com testes |
| `validador` | Confere de forma independente os critérios de aceite de uma tarefa |
| `guardiao` | Confere as regras invioláveis antes de cada commit |
| `analista-relacionamentos` | Resolve candidatos ambíguos de relacionamento (fase 2) |
| `documentador` | Escreve fichas de tabela ou de rotina a partir de um lote (fases 3 a 5) |
| `revisor` | Revisa fichas e receitas contra o catálogo, sem ter escrito o lote |
| `redator-obsidian` | Escreve as notas enxutas do vault do Obsidian a partir das fichas (fase 7) |

Subagentes não disparam outros subagentes: só a sessão principal (o orquestrador) dispara agentes.
