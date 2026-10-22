# Prompt inicial

Como começar:

1. Crie a pasta de trabalho (por exemplo `C:\dev\atlas`) e clone os dois repositórios dentro dela:
   ```powershell
   cd C:\dev\atlas
   git clone https://github.com/VgavaBR123/pg-atlas.git
   git clone https://github.com/VgavaBR123/atlas-eldados.git
   ```
2. Abra o Claude Code **nessa pasta** (`C:\dev\atlas`, não dentro de um dos repositórios).
3. Cole o prompt abaixo.

O `.env` com a conexão pode ser preenchido antes ou depois: sem ele, o Claude Code constrói tudo o que não depende do banco e deixa a pendência registrada.

---

```text
Você está na pasta de trabalho do projeto pg-atlas. Ela contém dois repositórios:
pg-atlas (ferramenta genérica, público) e atlas-eldados (documentação do banco, privado).

Trabalhe de forma totalmente autônoma. Não me faça perguntas: dúvidas vão para o agente
decisor, conforme pg-atlas/docs/AUTONOMIA.md. Só o que estiver na tabela de gates humanos
fica para mim, registrado em atlas-eldados/_controle/PENDENCIAS-HUMANAS.md, e o resto do
trabalho continua.

Passos:

1. Prepare a pasta de trabalho:
   powershell -ExecutionPolicy Bypass -File .\pg-atlas\scripts\preparar-workspace.ps1
   Isso copia CLAUDE.md, .claude/ (agentes e permissões) e .env.example para esta pasta.

2. Leia, nesta ordem: CLAUDE.md, pg-atlas/docs/PLANO.md, pg-atlas/docs/AUTONOMIA.md,
   pg-atlas/docs/FORMATO-DOCUMENTACAO.md, pg-atlas/docs/CUSTOS.md,
   atlas-eldados/docs/INSUMOS.md e os arquivos de atlas-eldados/config/.

3. Antes de qualquer outra coisa, crie atlas-eldados/_controle/custos/fases.jsonl e registre
   o início da fase 0 com data e hora. Faça isso manualmente agora; o coletor de custos será
   construído dentro da fase 0 e passará a fazer esse registro.

4. Execute a FASE 0 completa (itens 0.1 a 0.12 do PLANO.md), tarefa por tarefa, seguindo o
   laço da tarefa: implementador -> validador -> guardiao -> commit -> atualizar estado.
   Os agentes personalizados de .claude/agents/ só ficam disponíveis numa sessão nova; nesta
   sessão, use o agente de uso geral passando o conteúdo do arquivo do agente
   (.claude/agents/<nome>.md) como instrução de papel.
   Não leia nem exiba o arquivo .env em nenhum momento. Se ele não existir ou estiver
   incompleto, abra o gate G1 e continue com o que não depende do banco.

5. Quando a fase 0 estiver aprovada pelo validador e pelo guardião, registre o fim da fase 0,
   faça os commits (push do atlas-eldados só depois de confirmar que ele é privado) e inicie o
   loop autônomo numa janela própria, para que ele continue sem esta sessão:
   Start-Process powershell -ArgumentList '-NoExit','-Command','cd <esta pasta>; .\.venv\Scripts\python pg-atlas\tools\loop.py'
   Se o gate G1 ainda estiver aberto, inicie o loop mesmo assim: ele espera o .env existir,
   conferindo a cada 10 minutos, e segue sozinho depois.

6. Termine esta sessão com um resumo curto: o que foi construído, gates abertos e como
   acompanhar o andamento (atlas-eldados/_controle/ESTADO.md).
```

---

## Para acompanhar

- Andamento: `atlas-eldados/_controle/ESTADO.md`
- O que precisa de você: `atlas-eldados/_controle/PENDENCIAS-HUMANAS.md`
- Decisões tomadas sozinho: `atlas-eldados/_controle/DECISOES.md`
- Para parar o loop: crie um arquivo chamado `PARAR` na pasta de trabalho.
- Para retomar: apague o `PARAR` e rode de novo o comando do passo 5.
