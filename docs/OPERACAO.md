# Standby: operação

App de **contingência** do Genius Next (FastAPI + fila Procrastinate) rodando no **Render** com
banco no **Neon**. Estado em 03/nov/2026. O app de produção continua sendo o
`geniu5-ai/Genius-Next` na VPS 002; a base de conhecimento (armadilhas, runbooks) vive lá:
`docs/TRAPS.md` (#78 a #82 são deste app) e `docs/HANDOFF.md`.

Papel: ficar pronto para assumir se o agente principal cair. Por isso roda em `record_only` e só
deve ir a `live` num failover deliberado (ver "Pendente antes de atender cliente real").

## Onde está

| Peça | Valor |
|---|---|
| Serviço Render | `standby` (`srv-dbjvbvtg1s2s738rr2t0`), plano free, deploy automático a cada push na `main`. Renomeado em 03/nov/2026 (antes `genius-next-serverless`) |
| URL | `https://genius-next-serverless.onrender.com` (`/health`, `/webhook/evolution`, `/webhook/zernio`, `/v1/control/wake`) |
| Banco | Neon, projeto `Next` (`noisy-fire-02526952`), região `aws-sa-east-1`, endpoint `ep-spring-brook`, schema `genius_next` + tabelas `procrastinate_*` |
| Observabilidade | Logfire (`LOGFIRE_TOKEN`), projeto `genius` |
| Repositório | `geniu5-ai/standby` (antes `genius-next-serverless`) |

**Sobre a URL:** o Render não permite trocar o subdomínio de um serviço existente, então a URL
continua com o nome antigo mesmo após renomear o serviço. Para ter `standby.onrender.com` seria
preciso criar um serviço novo (copiar variáveis, repontar webhooks) e apagar o antigo. Nada em
produção aponta para esta URL hoje, só os workflows `monitor.yml` e `wake.yml` e o
`scripts/testar_webhook.sh`.

O plano free **dorme após ~15 min sem tráfego de entrada**: a primeira chamada leva até ~60 s (o
"Shutting down" no log é isso, não queda). Detalhes e a solução em [`WAKE.md`](WAKE.md).

## Variáveis de ambiente (nomes; os valores ficam só no Render)

| Variável | Para quê |
|---|---|
| `DATABASE_URL` | Neon |
| `HMAC_SECRET` | assinatura do `/webhook/evolution` (`X-Hub-Signature-256` ou `X-Evolution-Token`) |
| `CONTROL_HMAC_SECRET` | assinatura de `/v1/control/*`, incluindo `/v1/control/wake`; sem ele esses endpoints respondem 503 |
| `EVOLUTION_URL`, `EVOLUTION_API_KEY` | Evolution (histórico e "digitando"); com `record_only` nada é enviado |
| `OMNIROUTE_API_KEY` | OmniRoute (resumos, motor legacy) |
| `REPLY_ENGINE=agent`, `REPLY_AGENT_MODEL=openai:gpt-pool` | motor de resposta = Pydantic AI pelo combo `gpt-pool` do OmniRoute (o mesmo da produção, TRAPS #68 do Next) |
| `OPENAI_BASE_URL=https://omni.geniuslabs.pro/v1`, `OPENAI_API_KEY` | como o Pydantic AI alcança o OmniRoute |
| `OUTPUT_GUARD_ENABLED=true` | guarda de saída local (credencial, texto invisível) |
| `LOGFIRE_TOKEN` | envio para o Logfire |

**Não definidas de propósito:** `DELIVERY_MODE` (padrão `record_only`: a resposta é gravada no `outbox` e **nada vai ao WhatsApp**), `DELIVERY_ALLOWLIST` (vazia: o "digitando" também não sai), `OPS_ALERT_TO`, `HANDOFF_AUTO_RELEASE_TENANTS` (vazia: o aviso no boot é esperado enquanto não houver tenant real), `MANAGER_PENDENTES_TENANTS` (vazia: o `pendentes_sweep` não abordará ninguém).

## Segredos e credenciais (onde ficam; nunca os valores)

| Segredo | Onde | Observação |
|---|---|---|
| `HMAC_SECRET`, `DATABASE_URL`, `OMNIROUTE_API_KEY`, ... | variáveis do serviço no Render | lidas pela API do Render com a `RENDER_API_KEY` |
| `RENDER_API_KEY`, `NEON_API_KEY`, `OMNIROUTE_*`, `HMAC_SECRET` | `tokens.local` em `/home/ubuntu/.config/genius/tokens.local` nas VPS 002 e 003 | a `NEON_API_KEY` da **002** funciona (exige `org_id=org-flat-hat-11836365`); a da 003 está desatualizada |
| `WAKE_DATABASE_URL` | segredo do repositório no GitHub | URL do Neon do role `wake_ro`, **somente leitura**; uma linha só, começando em `postgresql://`, com `?sslmode=...` e sem aspas |
| `CONTROL_HMAC_SECRET` | segredo do GitHub **e** variável do Render | os dois com o mesmo valor |
| `TAILSCALE_AUTHKEY` | ambiente das sessões do Claude Code | entra na tailnet `topmidiahd` para acessar as VPS 002 (`n8n002`) e 003 (`camargorock`) por `tailscale ssh` |

Role do Neon `wake_ro`: `USAGE` nos schemas `public` e `genius_next` e `SELECT` apenas em
`procrastinate_jobs`, `procrastinate_workers`, `genius_next.events`, `genius_next.clients` e
`genius_next.outbox`. Não consegue escrever nem criar tabela.

## Tenant de teste

`mariaeduarda_ficticia` (instância `mariaeduarda-ficticia`, que **não existe** no Evolution):
`manager_jid` nulo (handoff não avisa ninguém), follow-up desligado em `record_only`, 4 perfis de
prompt copiados do tenant `genius` do Next (`reply_normal` v32, `persuasao` v5, `guardrail` v2,
`criticas` v8) e 11 itens de catálogo com **valores fictícios**.

## Testar

```bash
HMAC_SECRET=... ./scripts/testar_webhook.sh mariaeduarda-ficticia 5500000000010 "Quanto custa a prótese flexível?"
```

Resposta `202` com `workflow_id` = entrou. A resposta do agente sai em ~20 s:

```sql
select status, error, decision->>'action', decision->>'draft_text' from genius_next.turns
 where tenant_id = 'mariaeduarda_ficticia' order by created_at desc limit 3;
select delivery_mode, delivery_status, payload->>'text' from genius_next.outbox
 where tenant_id = 'mariaeduarda_ficticia' order by created_at desc limit 6;
```

Tudo deve estar `record_only` / `recorded`. Nunca use número real de WhatsApp como cliente de teste.

Teste de ponta a ponta feito em 02/nov/2026 (Render + Neon + modelo real): mensagem "Oi! Quanto custa a
prótese flexível?" → turno `completed`, resposta com o preço do catálogo (R$ 1.500 em até 10x) e
`outbox` em `record_only` / `recorded`.

Sem rede e sem banco (modelo falso, guardrails e agente): `pytest -q tests`.

## O que já deu errado e por quê

| Sintoma | Causa | Correção |
|---|---|---|
| `401` em todo webhook | Teste na URL da VPS (`genius-next.geniuslabs.pro`), não do Render; e `tenant_registry` vazio | URL do Render; cadastrar a instância |
| `503`, nada gravado | `clients_status_check` do Neon aceitava `new/off` em vez de `open/block` | Constraint igual ao baseline (Next TRAPS #82) |
| Turno `failed`: `DEEPSEEK_API_KEY` | Motor `agent` com o modelo DeepSeek padrão, sem a chave | `REPLY_AGENT_MODEL=openai:gpt-pool` + `OPENAI_*` |
| Toda resposta virava "Vou confirmar isso com a equipe" | Guarda de saída dependia do sidecar llm-guard, inexistente no Render | Guarda local + `OutputGuardrail` (PR #1; Next TRAPS #78) |
| Motor `legacy` respondia `action: "function"` | `gpt-oss` não segue o envelope JSON do legacy | Usar o motor `agent` |
| Workflow `Wake do worker` falhou com `ProgrammingError` | Segredo `WAKE_DATABASE_URL` colado com prefixo `NOME=`, aspas, espaço ou quebrado em duas linhas (sem o `?` antes de `sslmode`): o psycopg recusa a URL | Script tolera prefixo, aspas e espaços (PR #5); a quebra de linha precisa ser corrigida no segredo |
| `import app.wake` no Actions falhava com `No module named 'app.durable'` | `app/__init__.py` importa o app inteiro e o Actions só instala o `psycopg` | `scripts/wake_check.py` carrega `app/wake.py` pelo caminho |

## Wake do worker (Actions)

Resumo; o desenho completo, as decisões e as alternativas estão em [`WAKE.md`](WAKE.md).

O Procrastinate roda dentro do serviço web: no plano free ele dorme com o Render e leva os relógios
junto. `.github/workflows/wake.yml` (a cada 20 min, minutos 7/27/47) roda `scripts/wake_check.py`,
que consulta o Neon com o role somente leitura e, só se houver trabalho vencido, chama
`POST /v1/control/wake` assinado. Sem trabalho, o serviço continua dormindo.

| Onde | Nome | O quê |
|---|---|---|
| Segredo do GitHub | `WAKE_DATABASE_URL` | URL do Neon com o role `wake_ro` |
| Segredo do GitHub | `CONTROL_HMAC_SECRET` | o mesmo valor definido no Render |
| Variável do Render | `CONTROL_HMAC_SECRET` | habilita `/v1/control/wake` (sem ele responde 503) |
| Variável do GitHub (opcional) | `WAKE_TENANTS` | tenants separados por vírgula; vazio = todos |

Teste manual: *Actions → Wake do worker → Run workflow* na `main`, com `force`. Resultado
esperado: job verde e `{"status": "awake", "jobs_vencidos": 0, ...}`. Verificado em 03/nov/2026.

## Pendente antes de atender cliente real

1. Trocar as chaves que circularam em 02/nov (`HMAC_SECRET`, senha do Neon, OmniRoute, Evolution, `LOGFIRE_TOKEN`). Também as geradas para o wake (`wake_ro`, `CONTROL_HMAC_SECRET`) apareceram numa conversa e valem ser rotacionadas.
2. Embeddings: a busca de lições e o status audit chamam um sidecar de embedding local que não existe no Render (não derruba o turno, só não tem esse contexto).
3. Plano pago (o free dorme), `DELIVERY_MODE=live` + `DELIVERY_ALLOWLIST`, `HANDOFF_AUTO_RELEASE_TENANTS` com o tenant, e cadastro pelo runbook de ativação do Next (`docs/runbooks/RUNBOOK-ATIVACAO-TENANT.md`).
4. Failover automático: o workflow que vigia a VPS 002 e liga o `live` só quando ela cair ainda **não existe** (ver `WAKE.md`, "Próximos passos").
5. Investigar o job `conversation_turn` em `failed` de 02/nov 05:49 UTC (ainda não analisado).
6. Rotinas do Next que mencionam o nome antigo (`geniu5-ai/genius-next-serverless`) fora deste repositório: `scripts/core/dbos/github_monitor.py` no Genius-Dev (VPS 003) e `docs/HANDOFF.md` / `docs/TRAPS.md` no Genius-Next (VPS 002). Atualizar junto com o rename do repositório.
