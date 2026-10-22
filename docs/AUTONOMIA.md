# Execução autônoma

O dono do projeto não quer responder perguntas durante a execução. Este documento define como o Claude Code trabalha sozinho do início ao fim: quem decide no lugar do dono, como cada tarefa é validada, como o trabalho continua entre sessões e quais são as poucas situações em que ele precisa de uma ação humana.

## Os três laços

```
LAÇO EXTERNO (tools/loop.py)  — mantém o trabalho andando entre sessões
  └─ LAÇO DA TAREFA (orquestrador) — executa uma tarefa até ser aprovada
       └─ LAÇO DE DECISÃO (agente decisor) — responde dúvidas no lugar do dono
```

### 1. Laço externo: `tools/loop.py`

Script Python no pg-atlas, executado no workspace, que roda o Claude Code em modo não interativo repetidamente até o plano acabar. É o padrão conhecido como "Ralph loop": cada volta é uma sessão nova, que lê o estado em disco e faz a próxima coisa.

A cada volta:

1. Se existir o arquivo `PARAR` no workspace, encerra.
2. Lê `atlas-eldados/_controle/estado.json`. Se todas as fases estiverem concluídas, gera o relatório de custos e encerra.
3. Se a próxima tarefa depende do banco e o `.env` não existe ou está incompleto (testando só se as variáveis estão vazias, sem ler os valores), espera 10 minutos e volta ao passo 1.
4. Chama `claude -p` com o prompt fixo de continuação (abaixo), as permissões de `.claude/settings.json` e saída em JSON.
5. Registra o resultado da volta em `_controle/loop.log` (início, fim, tarefa, status).
6. Se a saída indicar limite de uso da conta, espera até o horário de liberação informado (ou 30 minutos, se não houver horário) e tenta de novo. O script não troca para a API sozinho (ver gate G4).
7. Se três voltas seguidas terminarem sem avanço no `estado.json`, para e registra a pendência G5.
8. Limite de segurança: no máximo 500 voltas por execução (configurável).

Prompt fixo de continuação:

```
Você está no workspace do pg-atlas. Leia CLAUDE.md, depois atlas-eldados/_controle/ESTADO.md
e atlas-eldados/_controle/estado.json. Execute a PRÓXIMA tarefa pendente do plano
(pg-atlas/docs/PLANO.md) seguindo o laço da tarefa descrito em pg-atlas/docs/AUTONOMIA.md.
Não pergunte nada ao usuário: dúvidas vão para o agente decisor. Ao terminar a tarefa,
atualize ESTADO.md e estado.json, faça o commit e encerre a sessão.
```

O Claude Code que implementa a fase 0 deve conferir as opções exatas do modo não interativo com `claude --help` antes de escrever o script (nomes de flags, formato de saída, modo de permissão) e documentar no próprio script a versão usada.

O loop é iniciado em uma janela própria, para continuar rodando independentemente da sessão que o iniciou:

```powershell
Start-Process powershell -ArgumentList '-NoExit','-Command','cd <workspace>; .\.venv\Scripts\python pg-atlas\tools\loop.py'
```

### 2. Laço da tarefa (orquestrador)

A sessão principal do Claude Code é o orquestrador. Ela não escreve código de produção diretamente quando a tarefa é grande. Ela delega e confere.

```
escolher tarefa ─► implementador ─► validador ──aprovado──► guardiao ──aprovado──► commit + estado
                        ▲               │                        │
                        └──reprovado────┘ (máx. 3)               └─reprovado─► implementador
```

1. **Escolher:** a primeira tarefa não concluída em `estado.json`, na ordem do `PLANO.md`.
2. **Implementar:** agente `implementador`, com a descrição da tarefa e os critérios de aceite copiados do plano.
3. **Validar:** agente `validador`, que não viu a implementação ser feita. Roda testes, executa os comandos, confere cada critério de aceite e devolve `APROVADO` ou `REPROVADO` com a lista de problemas.
4. **Reprovado:** volta ao implementador com a lista, no máximo três vezes. Na terceira reprovação, a tarefa é marcada como `bloqueada`, o motivo vai para `PENDENCIAS-HUMANAS.md` e o orquestrador passa para a próxima tarefa que não dependa dela.
5. **Guardar:** agente `guardiao`, que confere as regras invioláveis no que vai ser commitado (ver lista no `CLAUDE.md` do workspace).
6. **Concluir:** commit no repositório certo, atualização de `estado.json` e de `ESTADO.md` (resumo legível do andamento), marca de fim no coletor de custos se a fase terminou.

Nas fases 3, 4, 5 e 7, que produzem documentação em vez de código, o laço é o mesmo com outros papéis: `documentador` (ou `redator-obsidian`, na fase 7) no lugar do implementador, `revisor` no lugar do validador, mais as verificações mecânicas do `pg_atlas`.

### 3. Laço de decisão (agente `decisor`)

Toda vez que o orquestrador ou outro agente teria que perguntar algo ao dono, chama o `decisor`. Ele:

1. Lê a seção "Política de decisões" deste documento, o plano, a configuração e as decisões anteriores (`_controle/DECISOES.md`).
2. Decide, escolhendo a opção mais segura e mais reversível que cumpra o plano.
3. Registra a decisão em `DECISOES.md`, no formato: número, data, pergunta, opções consideradas, escolha, motivo, como reverter.
4. Só devolve "precisa do dono" quando a pergunta cai num dos gates da lista abaixo. Nesse caso, registra em `PENDENCIAS-HUMANAS.md` e indica ao orquestrador o que pode continuar enquanto isso.

## Política de decisões

O decisor segue estas regras, em ordem de prioridade:

1. **As regras invioláveis vencem tudo:** somente leitura, uma conexão, nenhum dado pessoal, nenhuma credencial em arquivo, pg-atlas genérico.
2. **Entre duas opções, escolher a reversível.**
3. **Entre duas opções reversíveis, escolher a mais simples** que atenda ao critério de aceite.
4. **Dúvida sobre o significado de um dado do banco** não se decide por suposição: vira hipótese verificável (fila de verificação) ou entrada em `duvidas.md`.
5. **Dúvida de ferramenta ou biblioteca** (por exemplo, "o pacote X não instala no Windows"): usar o fallback previsto no plano. Se não houver, escolher a alternativa pura em Python e registrar.
6. **Dúvida de formato** (nome de arquivo, organização de pastas): seguir `FORMATO-DOCUMENTACAO.md`. Se o caso não estiver previsto, decidir, registrar e aplicar igual em todo o projeto.
7. **Nunca decidir sozinho:** gastar dinheiro, liberar leitura de valores de tabela, publicar em repositório público, apagar histórico do git.

## Gates humanos

São as únicas situações que dependem do dono. Nenhuma delas para o trabalho inteiro: o orquestrador segue com o que não depende do gate.

| Gate | Quando | O que o dono faz | O que continua enquanto isso |
|---|---|---|---|
| G1 | Não há `.env` preenchido na pasta de trabalho | Copia `.env.example` para `.env` e preenche (ver `atlas-eldados/docs/INSUMOS.md`) | Toda a fase 0 que não usa banco; testes offline |
| G2 | Não foi possível confirmar que o atlas-eldados é privado | Confirma no GitHub e marca a pendência como resolvida | Commits locais; o push fica suspenso |
| G3 | Há tabelas candidatas a domínio | Copia para `config/dominios.yml` as que aprova | Tudo; as tabelas não aprovadas são documentadas só pela estrutura |
| G4 | Limite de uso da conta atingido várias vezes seguidas | Decide se autoriza o uso da API (com teto) | O loop espera a liberação do limite e continua sozinho |
| G5 | O loop parou de avançar | Lê `PENDENCIAS-HUMANAS.md` e `loop.log` | Nada; o loop está parado |

Para resolver um gate, o dono edita `PENDENCIAS-HUMANAS.md` trocando `ABERTA` por `RESOLVIDA` na linha correspondente. O loop confere esse arquivo a cada volta.

## Arquivos de controle (em `atlas-eldados/_controle/`)

| Arquivo | Para quê |
|---|---|
| `estado.json` | Estado de máquina: fase atual, tarefas e itens concluídos, retomada |
| `ESTADO.md` | O mesmo estado, legível: o que foi feito, o que está em andamento, próximos passos |
| `DECISOES.md` | Todas as decisões tomadas pelo decisor |
| `PENDENCIAS-HUMANAS.md` | Gates abertos e resolvidos |
| `loop.log` | Uma linha por volta do laço externo |
| `custos/` | Marcas de início e fim de fase e relatórios de uso |
| `lotes/`, `verificacoes/` | Pacotes de contexto e fila de verificações das fases 3 a 5 |
| `db.lock` | Trava de conexão única (não versionado) |
