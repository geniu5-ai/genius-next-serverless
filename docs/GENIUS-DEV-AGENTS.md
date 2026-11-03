> **Cópia de referência (03/nov/2026).** Fonte: `Genius-Dev/AGENTS.md` na VPS 003 (constituição e regras da casa do Genius Dev).
> A fonte é o arquivo lá; **não edite esta cópia**, atualize-a copiando de novo. Nada neste arquivo é segredo.

# AGENTS.md — índice e regras da casa do Genius Dev

Todo agente que trabalha neste repositório lê este arquivo: Hermes, Claude Code, OpenCode, CommandCode,
Aider, Senior e guardiões. As CLIs o carregam sozinhas a partir da raiz.
Ele é um **índice**: diz onde está cada coisa e traz só as regras que valem sempre. O detalhe mora no
documento indicado ou no PG. Acima dele só a constituição (`docs/manual/11-governanca/protocolo/01-constituicao.md`,
fonte em `crew.docs`); se algo aqui a contradisser, ela vence.

## 1. Para fazer X, consulte Y
Os buttons ficam em `~/.local/bin` (catálogo em `features/buttons/catalog.json`).
Mantenha este índice vivo: criou ou moveu algo no Dev, atualize a linha correspondente e o doc do PG.

| Para… | Consulte ou use |
|---|---|
| Começar a sessão ou retomar trabalho | §7 abaixo e `docs/HANDOFF.md` |
| Saber onde mora o conhecimento (git x PG), o sync diário e como conferir | doc PG `conhecimento-git-pg-20261029`; `python3 scripts/ops/docs_git_sync.py verify` e `python3 scripts/rag/kb_pg.py verify --kb sales` |
| Saber o que existe no Dev, em que estado e qual a decisão para cada peça (catálogo) | doc PG `catalogo-dev-20261020` |
| Regras de organização, ciclo de vida (ATIVO, PAUSADO, ARQUIVADO) e controle de mudança do Dev | doc PG `governanca-dev-politica-20261020` |
| Ver o que espera pelo chairman (decisões, ordenadas por impacto) | doc PG `painel-chairman` |
| Distinguir os termos "Radar" (tecnologia) e Genius-Sites (produto de venda de sites, antes Genius Maps / Genius Radar) | `docs/HANDOFF.md`, seção "Glossário" |
| Ver a fila quente (p0/p1 abertas) | button `hotq` |
| Ver o que mudou nas últimas horas | button `since [horas]` |
| Criar, pegar ou fechar task | `python3 scripts/pg_write.py task …` (§2) |
| Gravar decisão, lição, runbook, incidente ou pesquisa | `pg_write.py doc write` (§2) |
| Ler um doc do PG | button `docget <id>` ou `pg_write.py doc get` |
| Buscar conhecimento por significado (RAG) | button `kbq "<pergunta>"` (cards de vendas + docs da casa, saída curta, responde abaixo de 1s via `genius-embed.service`, porta 8721; cai sozinho no carregamento local ~14s se o serviço estiver fora); `ragup "<pergunta>"` (só docs/tasks, JSON); memória de sessões: `memq` |
| Consultar ou ampliar a base de conhecimento de vendas (cards, fontes, livros) | `features/knowledge/sales/README.md`; cards e validação em `scripts/rag/genius_kb.py`, índice no PG em `scripts/rag/kb_pg.py` (Qdrant apagado em 29/out/2026); integridade em `tests/test_sales_kb.py` |
| Consultar a curadoria de posts do Instagram por setor (MKT, VENDAS, SEO, IA): um post abaixo do outro, sem mesclar | `features/knowledge/instagram/README.md` (fonte: `genius_dev.instagram_posts`; manifesto `_fontes.json`; integridade em `tests/test_ig_knowledge.py`; RAG ainda não) |
| Consultar o PG | button `pgq "<sql>"` |
| Ver a saúde do DBOS e dos serviços | buttons `dbst` e `failed` |
| Sincronizar o banco de teste `genius_dev_test` com o schema `crew` de produção (tabela criada fora do Alembic, teste falhando por tabela ausente) | `python3 scripts/ops/sync_test_db.py [--dry-run]` (só estrutura e linhas de config, nunca dados); seção "Banco de teste" em `scripts/alembic/README.md`; `docs/TRAPS.md` #108 |
| Diagnosticar um sintoma | `docs/TROUBLESHOOTING.md` (índice por sintoma) |
| Não repetir erro conhecido | `docs/TRAPS.md` |
| Saber por que a 003 caiu em 20/out (OOM do `claude` acordado pelo DBOS) e o limite de memória dos serviços que disparam CLI de agente | `docs/TRAPS.md` (OOM da 003) |
| Emergência, backup e restore | `docs/manual/11-governanca/protocolo/03-runbooks.md` |
| Mexer em workflow ou schedule do DBOS | `scripts/core/dbos/schedules.py` e `docs/TRAPS.md` (schedules vivem no banco) |
| Mudar contrato de entrada ou saída | `scripts/crew_contracts.py` |
| Mudar o contexto injetado nos agentes | `scripts/core/bootstrap.py` e `agents/<nome>/prompt.md` |
| Rotina de um agente próprio | `agents/<nome>/checklist.md` (CEO: `agents/claude/checklist.md`) |
| Ver o contrato e as permissões do Infra (mãos no host: monitora, sobe o que caiu, limpa) | `.opencode/agents/infra.md`; decisão, provas e pendências: doc PG `agente-infra-nativo-opencode-20261003` |
| Criar ou revisar uma machine (automação sem LLM) | `features/machines/README.md` e `docs/manual/04-machines/`; registro em `crew.machines`, runs em `crew.machine_runs` (`scripts/ops/seed_machines.py`) |
| Trocar um termo ou nome em vários arquivos, docs e no PG (rename de produto, símbolo Python, blocos compartilhados dos prompts) | botão `renames`: `.venv/bin/python -m features.machines.codemod_machines renames <rules.yaml>` (prévia; `--apply`, `--commit` e `--apply-pg` gravam); exemplo `features/machines/codemod_machines/rules/genius-sites.yaml`; guia, quando usar e erros: doc PG `machines-codemod-guia-20261017` |
| Saber quais tasks são voltas de loop (rondas, `[SRE]`, `*_watch`, `[sonda-cega]`, self-care), como tratá-las e o catálogo dos loops | doc PG `loops-da-casa-20261028` |
| Ver o veredito das sondas (armadilhas, guardiões, pulso da crew e goss da 003) | `SELECT report_json FROM crew.machine_runs WHERE machine IN ('traps_watch','guardian_probes','crew_pulse','infra_goss') ORDER BY id DESC LIMIT 3` (o `crew_pulse` traz schedules DBOS parados e o SLO da crew; o `infra_goss` é o `goss.yaml` da VPS003); relatório do dia: doc PG `relatorio-dev-rotinas-20261001` |
| OpenCode ou guardião com timeout 1800s | `docs/TRAPS.md` 7d-7f; modelo dos wakes em `scripts/utils/cli_wake.py` (doc PG `runbook-modelos-e-hermes-home`) |
| Saber qual modelo cada agente usa, free tier da OpenAI e freio de tokens | doc PG `politica-modelos-e-provedores-20261001`; estado ao vivo: `curl -s 127.0.0.1:8090/guard` e machine `provider_health` |
| Ver o painel de comando do Dev (machines, DBOS, crew, infra, self-heal) | https://dashboard.geniuslabs.pro/monitor/; arquitetura e rollback: doc PG `genius-control-monitor-20261005`; produtor em `products/genius_control/` |
| Gestar e monitorar o Genius Next (002) | doc PG `governanca-genius-next-002-20261001`; veredito: `SELECT report_json FROM crew.machine_runs WHERE machine='next_watch' ORDER BY id DESC LIMIT 1`; tasks `[next_watch]` e `[next]` vão para o Hermes |
| Operar o Kyrios (agente Júlia): ponte Zernio, prompt, follow-up e vigia | doc PG `genius-next-zernio-kyrios-20261008`; no Next (`ssh n8n002`, `~/Genius-Next`): `docs/HANDOFF.md` (bloco Kyrios), `docs/TRAPS.md` #23-#31, `docs/TROUBLESHOOTING.md` §11, `deploy/ops/kyrios_watch.sh`; monitor durável: task #8750 |
| Gerar o relatório de atendimentos (PDF) para o cliente ou o owner | doc PG `padrao-relatorio-atendimentos-cliente-20261013`; código em `features/reports/atendimentos/`; saídas em `~/relatorios/` (fora do git, tem dado de paciente) |
| Backup e recuperação da VPS 003 (restic + rclone no R2, provisionamento e restore por Ansible) | `features/backup/README.md`; executor `features/backup/bin/restic_backup.sh`; playbooks em `features/backup/ansible/`; doc PG `backup-restic-rclone-ansible-20261015` |
| Garantir que o Genius não caia (Genius-Heal: auto-heal e recovery, `flow_state=pending`, agente `contingency`) | doc PG `genius-heal-canonico` (fonte única); entrada no repo `products/genius-heal/README.md`; reserva do Next: `next-contingencia-failover-20261029` |
| Ligar ou desligar a suíte inteira (mínima x full) | `python3 scripts/ops/suite_mode.py status\|minimal\|full [--dry-run]`; doc PG `modo-suite-minima-20261002` |
| Analisar perfil ou post do Instagram (ranking por engajamento + análise com IA por tenant) | buttons `sapost`, `saprofile`, `sarun`; skill `.agents/skills/social-analyzer/SKILL.md`; detalhe e custos em `products/social_analyzer/README.md`; estado dos provedores: `GET /v1/providers` |
| Conectar agentes de IA ao Analyzer (MCP em `/mcp`, chaves por marca, equipe e exclusão de conta) | `products/social_analyzer/README.md` (seções "Integração com agentes de IA" e "Equipe, marcas e conta"); skill em `.agents/skills/genius-analyzer-prospeccao/SKILL.md`; SQL versionado em `products/genius-intel/social-analyzer/accounts.sql` |
| Criar um button | `features/buttons/README.md` |
| Usar as ferramentas de terminal (rg, fd, jq, xh…) | `features/libs/CHEATS.md` |
| Conectar o Google (Ads, GA4, Perfil da Empresa, Search Console) de um cliente | doc PG `genius-ads-google-ads-conexao-guia-20261015` (passo a passo e erros); plano `genius-ads-planejamento-conexoes-google-20261015`; inventário de APIs `genius-ads-apis-google-ativas-20261015`; script `products/genius-ads/tools/ads_cli.py`; feature `features/google-connect/README.md` |
| Radar único de rotas, modelos e provedores (rotas do roteador local geradas do yaml, combos do Omni, vivos e mortos, armadilhas) | `agents/router/REFERENCIAS.md` (só modelo testado; `OMNI_MODELS.md` virou ponteiro) |
| Ver/ajustar a lista grátis do combo dinâmico `auto/julia` do Omni (machine `omni_free_pool`, testa e regrava `models` a cada 3h) | `features/machines/omni_free_pool/` (padrões em `candidatos.yaml`); run em `crew.machine_runs` |
| Aider do Senior (editor de código, DeepSeek 4.1 Flash via Token Harbor, na 002) | `agents/senior/bin/senior-aider` e `agents/senior/aider/.aider.conf.yml`; armadilhas TRAPS #72 e #73 |
| Operar o Omni (hospedagem, túnel, saúde, editar combo) | `features/router/README.md` (rotina do agente: `agents/router/checklist.md`; `OPERATIONS.md` virou ponteiro) |
| Roteador local da 003 (freio diário da OpenAI por conta, reservas, porta 8090) e suas rotas | `scripts/llm_router/README.md`; rotas em `scripts/llm_router/llm_route.example.yaml`; tabela: `python -m scripts.llm_router.rotas` |
| Combos do Omni (`gpt-pool`, `media-vision`, `media-audio`), contas duplas, áudio e mídia do Next, OpenRouter free e armadilhas | doc PG `omni-combos-audio-midia-20261019`; testes e números em `agents/router/REFERENCIAS.md`; prompt do agente `agents/router/prompt.md` |
| Escolher provedor ou conta de LLM, agregar conta nova, combos do Omni (analyzer) | doc PG `estrategia-provedores-llm-20261004`; `features/router/README.md` §7; armadilhas `docs/TRAPS.md` #31-#40 |
| Testar a segurança de um prompt | `features/security/README.md` |
| Assinatura eletrônica (DocuSeal) | `features/docuseal/README.md` |
| Evolution 2.4 de validação (isolada da prod) | `features/evolution/README.md` |
| Vaultwarden de teste | `features/vaultwarden/README.md` |
| Skill Exa (busca/API) | `.agents/skills/build-with-exa/SKILL.md` |
| Trabalhar no Genius Next | `ssh n8n002` (`~/Genius-Next`); aqui fica só `docs/architecture/genius-next-flow.html` |
| Conectar uma sessão Claude cloud (claude.ai/code) à VPS 003 via Tailscale | doc PG `runbook-tailscale-sessao-cloud-20261006` (`pg_write.py doc get --slug …`) |
| Decidir algo grande ou irreversível | constituição e Conselho (`crew.conselho`) |
| Saber o estado geral e o parecer do dia | doc PG `relatorio-29set-2026` (`pg_write.py doc get --slug relatorio-29set-2026`) |
| Trocar o modelo de um agente ou mexer no Hermes | doc PG `runbook-modelos-e-hermes-home` |
| Saber o plano vigente | `docs/plano/2026-09-29-auditoria-relogio-suico.md` e doc PG `plano-relogio-suico-checklist` |
| Trabalhar o SEO e o marketing da CLV Advocacia (cliente premium) | estado e regras: doc PG `clv-seo-brief-crew-20261007`; o que publicar no Perfil do Google, no Google Ads e no site: `clv-mkt-brief-execucao-20261007`; palavras-chave e pautas: `clv-mkt-palavras-chave-20261007`; auditoria local: `clv-auditoria-local-20261007`; código em `~/Medicwave/clientes/clv/seo/` e `~/Medicwave/clientes/clv/marketing/`; regras da OAB em `clv-seo-acoes-externas-20261007`; Google Ads, acesso e roteiro para qualquer cliente novo (vincular à MCC Medicwave): `clv-google-ads-onboarding-20261014` |

## 2. Núcleo inegociável: estado
- **PostgreSQL** `genius_dev` é a verdade. Schemas `crew` (domínio) e `dbos` (execução).
- **DBOS** executa: workflows duráveis, filas e schedules. Decisão fechada: não proponha Temporal, Windmill ou Prefect.
- **Pydantic** na borda: contratos de entrada e saída (`scripts/crew_contracts.py`, `AgentOutput`).
- **Agendamento:** nenhum cron, timer systemd ou scheduler fora do DBOS. Todo job é workflow em `scripts/core/dbos/schedules.py` (ordem do chairman, 29/set).
- **Tasks:** a fila única é `crew.tasks`. Criar: `python3 scripts/pg_write.py task --agent <a> --title "..." --body "..." --priority p2`.
  Toda escrita em `crew.tasks` usa `WHERE id = %s`. GitHub Issues não é fila.
- **Conhecimento** (decisão, lição, runbook, incidente, pesquisa) vai para o PG, não para `.md` novo:
  `echo "texto" | python3 scripts/pg_write.py doc write --slug <slug> --title "..." --tags <tags>`.
  Consultar: `python3 scripts/pg_write.py doc list --tag <tag>` e `doc get`.

## 3. Arquivos `.md` permitidos
Os `.md` que já existem continuam. **Novo** `.md` só nestes lugares; o resto vai para o PG:
- raiz: `AGENTS.md`, `CLAUDE.md`, `README.md`
- `agents/<agente>/`: `prompt.md`, `checklist.md`, `REFERENCIAS.md`; Prompter: `agents/prompter/library/**` (docs operacionais de prompt)
- `.opencode/agents/<agente>.md` (contrato de invocação dos agentes nativos do OpenCode: frontmatter + permissões; o prompt canônico continua em `agents/<agente>/prompt.md`)
- `.claude/agents/<agente>.md` (contrato de invocação dos agentes que rodam no Claude Code CLI, como o `contingency`; o prompt canônico fica em `agents/<agente>/` ou, se o agente é de um produto, em `products/<produto>/agents/<agente>/` com `prompt.md` e `checklist.md`)
- `docs/HANDOFF.md`, `docs/TRAPS.md`, `docs/TROUBLESHOOTING.md` e `docs/plano/`
- `features/<feature>/` e `products/<produto>/`: `README.md` e guias de operação
- `docs/manual/` (espelho do PG) e skills de terceiros (`.agents/skills/`)
Criar `.md` fora desta lista é erro: grave no PG com `pg_write.py doc write`.

## 4. Human-gate: sempre com o chairman
- Irreversível: apagar dado que não é seu (PG, R2, backups, workflows n8n), `DROP`, `TRUNCATE`, delete em massa.
- Dinheiro fora do combinado, ou qualquer coisa que saia da máquina para cliente ou terceiro.
- n8n `HHmfcsmJhirSVoE9` (Genius Sell, congelado): leitura livre, escrita proibida.
- Evolution (WhatsApp): agentes só fazem GET. **Nunca** DELETE, create, logout, restart ou connect de instância; instância WhatsApp só o chairman cria ou recria (o pareamento exige o celular). A instância `genius` não se recria. Evolution se reinicia com `docker service update --force`, nunca `docker restart`.
- Não mover, renomear nem apagar doc do Genius Next sem ordem; relatório novo vai para a pasta certa do `docs/HANDOFF.md` do Next.
- Nunca gravar senha, chave, código de pareamento nem telefone de cliente no PG, em doc ou em resultado de task.
  Exceção vigente (ordem do chairman, 31/out/2026): as contas de provedores são **sparring** (e-mail efêmero, cartão pré-pago). Ler o , testar e usar chave de conta de teste não exige cerimônia nem aviso; telefone e dado de cliente seguem proibidos. Doc PG .
- Push ou sync para fora, e instalar dependência nova: só com ordem explícita.
- Tasks com `agent='chairman'`: não tocar, só sinalizar.
- Mudar a constituição.
Reversível → aja, com backup antes e prova depois. Pedir permissão para consertar o que é reversível é falha.

## 5. Crew e hierarquia
Chairman (Camargo) → **Claude Code** (CEO técnico: comanda, decide, escreve spec e audita) → **Hermes**
(diretor executivo, executor e curador; porta de entrada pelo gateway/WhatsApp) → executores, guardiões e workers.
Emenda da constituição de 02/out/2026: o CEO e o Hermes são os donos do Dev e os guardiões do Genius Next (ver doc PG `governanca-genius-next-002-20261001`).

**Interinidade do CEO (ordem do chairman, 17/out/2026):** se o Claude Code estiver indisponível por falta de crédito ou limite de uso (`tail -5 /tmp/claude_wake_test.log` com `weekly limit`, `usage limit` ou `spend limit`, ou task p0/p1 do CEO parada há mais de 2 h), o Hermes assume a rotina executiva dele de forma interina: checklist operacional, `kyrios_watch`, `kymsgs` e decisões reversíveis com backup e prova. Continua com o chairman: irreversível, dinheiro, saída para terceiros, constituição, push, dependência nova e prompt de agente de cliente. Ao voltar, o CEO audita o doc de interinidade que o Hermes deixou no PG (tag `interinidade`). Não muda a hierarquia: o CEO segue acima do Hermes.

| Agente | Papel | Como chamar |
|---|---|---|
| Hermes | diretor executivo, executor e curador (responde ao CEO): recebe do chairman, quebra em tasks, distribui, cobra prova, cura docs | gateway, ou `hermes -z "..."` |
| Claude Code | CEO técnico: comanda, diagnostica, escreve spec, audita | `claude -p "..."` |
| OpenCode | executor geral | `opencode run --dir ~/Genius-Dev "..."` |
| Senior | executor cirúrgico (spec fechada; Aider só como ferramenta) | `opencode run --agent senior --dir ~/Genius-Dev "..."` |
| CommandCode | código pesado | `commandcode -p --yolo "..."` (sem `--yolo` trava) |
| core-guardian | saúde de PG, DBOS, Pydantic e backup | ronda horária (:07), fila `core-guardian` |
| mind-guardian | saúde de bootstrap, memória, loops e wakes | ronda horária (:37), fila `mind-guardian` |
| Infra | mãos no host (003 e n8n002): monitora, sobe o que caiu, limpa | fila `infra` (`opencode run --agent infra`) |
| Contingency | guardião da contingência e da recuperação de desastre (Genius-Heal): prontidão, drill, runbook de queda; promove a reserva só com aval do chairman | `claude -p --agent contingency` (fila `contingency` ainda não ligada: task #9528) |
| Genius | agente reflexivo do worker DBOS, sem ferramentas | fila `genius` |

## 6. Como trabalhar
1. **Prova ou não aconteceu:** arquivo no disco, linha no PG, commit, health check. "Pronto" sem prova vale zero.
2. **Escopo cirúrgico:** toque só o que a task pede. Commit só dos seus arquivos: `git add -- <arquivos>` e confira
   `git diff --cached --name-only` antes de commitar.
3. **Delegue com spec fechada** e peça só resultado + prova.
4. **Iniciativa se comunica, não se aplica:** viu caminho melhor? Termine o combinado e proponha no relatório.
5. **`commit` não é deploy:** compare `ExecMainStartTimestamp` do serviço com o último commit.
6. **Português correto.** Não imite o registro do chairman.
7. **Princípio do dono** (ordem do chairman, 02/out/2026): o CEO (Claude) e o Hermes são os donos do Dev e os guardiões do Genius Next, o primeiro produto; mais produtos virão. A cada problema, duas perguntas, nesta ordem:
   (a) **O que posso fazer para resolver, e como deixo este software melhor?** Tenho acesso a todas as ferramentas, chaves e repos: usar, sem esperar permissão para o que é reversível.
   (b) **Contraponto: se já está bom e funcionando, para que trocar ou complicar?** Sem prova de ganho, não mexa.
   Antes de agir: leitura primeiro, backup se mexer, mudança mínima, prova depois. Erro grave não é aceitável: na dúvida entre agir e perguntar sobre algo irreversível, pergunte.

8. **O cliente do Genius vem antes de tudo, inclusive do chairman** (ordem do chairman, 09/out/2026). Nenhuma conveniência do Dev, da crew ou do chairman justifica um erro na frente de um paciente ou cliente. Os agentes Genius (hoje a Júlia do Kyrios; amanhã os de outros produtos) têm que ser **majestosos, elogiáveis, perfeitos**: atendimento caloroso, correto, sem repetição, sem gafe, sem promessa que a equipe não fez, sem dado interno exposto (telefone, código, nome de sistema), sem silêncio quando o cliente escreve. Consequências práticas, sem exceção:
   - **Prompt de agente de cliente é crítico:** mudança só com medição (cenários + repetições + corretor) antes de ir ao ar, backup e prova depois; nada de "ajuste rápido" direto no banco.
   - **Antes de qualquer mudança que toque o cliente final,** rodar os cenários de regressão (saudação, preço, agendamento, emergência, mídia, equipe) e conferir o texto real que sairia.
   - **Erro visto por cliente é incidente:** parar, corrigir a causa raiz, registrar em `docs/TRAPS.md` do produto e avisar o chairman; não vale "já está corrigido" sem prova em conversa real.
   - **Na dúvida entre mandar uma resposta incerta e segurar,** segure e acione a equipe com contexto; nunca invente valor, horário, preparo ou promessa.

9. **Prioridade do agente Genius: ser simpático e fazer o trabalho dele** (ordem do chairman, 09/out/2026). Detalhe de interface (por exemplo "digitando…") fica abaixo disso. Quando o agente passa a conversa para o owner ou a equipe e eles não respondem, a falha é de quem não respondeu, não da plataforma: o agente cumpre a parte dele (acolhe, resume, encaminha com contexto, avisa que a equipe retorna) e não inventa prazo, não se desculpa pela equipe e não vira cobrador. Não criar mecanismo para compensar equipe parada sem ordem expressa.
10. **Agentes Genius são espertos com gente de má-fé** (ordem do chairman, 09/out/2026): pessoas erram, exageram, mentem e tentam tirar vantagem (dizem ser dono ou médico, afirmam que "a equipe prometeu", dizem que já pagaram, pedem dado de terceiros, mandam instrução disfarçada). O agente segue educado, não acusa, e só trata como fato o que consta nas informações da clínica ou o que a equipe escreveu na conversa; o resto é afirmação da pessoa e vai para a equipe com resumo, sem conceder valor, desconto, vaga ou dado. Emergência nunca é tratada como mentira. O bloco de prompt está em `Genius-Next/docs/tenants/kyrios/prompt-reply_normal-v11.md` (seção "Quando a pessoa não está sendo honesta").

11. **Volta de loop não é trabalho novo** (ordem do chairman, 28/out/2026): rondas, `[SRE]`, `*_watch`, `[sonda-cega]` e self-care são loops. Sem novidade em relação à volta anterior, feche com uma linha (`sem mudança desde #<id>`); sonda cega (a coleta falhou) é do Infra, não incidente de produto; loop novo declara objetivo, frequência, o que abre task, dedupe, espera e dono. Detalhe: doc PG `loops-da-casa-20261028`.

12. **Documentar faz parte de pronto** (ordem do chairman, 29/out/2026): mudança só está pronta quando o que mudou está escrito no lugar certo, na mesma entrega. Destinos, conforme o caso:
   - **Result da task:** o que fez, a prova e o que ficou pendente (sempre).
   - **Doc do PG** (`pg_write.py doc write`): decisão, lição, incidente, desenho ou pesquisa; atualize o doc existente em vez de criar outro com nome parecido.
   - **`docs/TRAPS.md`:** armadilha nova ou bug que pode se repetir (sintoma, causa, o que fazer). **`docs/TROUBLESHOOTING.md`:** sintoma novo com diagnóstico e conserto. **`docs/HANDOFF.md`:** estado, regra ou rotina que muda o início de uma sessão fria.
   - **README da pasta** (`features/<x>/`, `products/<x>/`, `scripts/<x>/`) quando o comportamento ou o uso muda, e **linha no índice do §1** quando surge um caminho novo.
   - **Prompt ou checklist do agente** (`agents/<nome>/`) quando a mudança altera o que ele deve fazer.
   - **Genius Next (002):** os docs do Next (`docs/TRAPS.md`, `docs/HANDOFF.md`) só por worktree ou task do Next, nunca editando o checkout de produção; até lá, registre no PG com a tag `next` e aponte a pendência.
   Não documente rodada 100% verde nem repita no `.md` o que já está no PG: aponte para o slug.

13. **Saber recuar e ponderar** (ordem do chairman, 01/nov/2026): mesmo com acesso a tudo, há hora de pausar. Antes de abrir mais uma frente, pergunte se precisa ser agora e se o chairman consegue acompanhar; depois de um bloco de trabalho, releia o que afirmou e separe o provado do suposto; menos mensagens e menos lembretes automáticos; diante de bloqueio de fora, espere com calma em vez de insistir em laço. Um dia de cada vez. Doc PG `licao-saber-recuar-e-ponderar-20261101`.

## 7. Boot de sessão
```bash
systemctl is-active genius-dbos-worker genius-dbos-api genius-dbos-schedules genius-scheduler
hotq                      # fila quente
since 12                  # o que mudou nas últimas 12 horas
git log --oneline -5
```

## 8. Onde mora cada coisa
- **Código e regras:** `~/Genius-Dev`. **Estado** (tasks, docs, lições, busca semântica): PG `genius_dev`.
- **Segredos:** só em `~/.config/genius/tokens.local`. Nunca no repo, em log ou em prompt.
- **Config e estado de cada CLI:** pasta nativa no home (`~/.hermes`, `~/.claude`, `~/.config/opencode`,
  `~/.commandcode`, `~/.aider`). **Agentes próprios:** `agents/<nome>/`. **Skills:** `.agents/skills/`.
  (Codex saiu da crew em 02/out/2026 — não há `agents/codex/` nem fila Codex.)
- **Hosts:** VPS003 (esta) roda o Genius Dev; n8n002 roda o Genius Next e o Genius-App; VPS001 roda o OmniRoute.

## 9. As armadilhas que mais custaram (lista completa em `docs/TRAPS.md`)
- `pgrep`/`pkill -f` casa com o próprio comando: use o PID exato.
- `UPDATE` em `crew.tasks` sem `WHERE id` sobrescreveu 475 tasks (12/set).
- `SUCCESS` ou `active` não provam nada: confira exit code, consulta ou arquivo.
- Schedules do DBOS vivem no banco (`dbos.workflow_schedules`): tirar do código não desliga.
- `DBOSClient(pg_url)` sem `system_database_url=pg_url` falha em silêncio.
