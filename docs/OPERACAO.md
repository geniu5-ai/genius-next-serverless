# Genius Next Serverless: operação

Cópia do Genius Next (FastAPI + fila Procrastinate) rodando no **Render** com banco no **Neon**.
Estado em 02/nov/2026. O app de produção continua sendo o `geniu5-ai/Genius-Next` na VPS 002;
a base de conhecimento (armadilhas, runbooks) vive lá: `docs/TRAPS.md` (#78 a #82 são deste app)
e `docs/HANDOFF.md`.

## Onde está

| Peça | Valor |
|---|---|
| Serviço Render | `genius-next-serverless` (`srv-dbjvbvtg1s2s738rr2t0`), plano free, deploy automático a cada push na `main` |
| URL | `https://genius-next-serverless.onrender.com` (`/health`, `/webhook/evolution`, `/webhook/zernio`) |
| Banco | Neon, projeto `ep-spring-brook` (sa-east-1), schema `genius_next` + tabelas `procrastinate_*` |
| Observabilidade | Logfire (`LOGFIRE_TOKEN`), projeto `genius` |

O plano free **dorme após ~15 min sem tráfego**: a primeira chamada leva até ~60 s (o "Shutting down" no log é isso, não queda).

## Variáveis de ambiente (nomes; os valores ficam só no Render)

| Variável | Para quê |
|---|---|
| `DATABASE_URL` | Neon |
| `HMAC_SECRET` | assinatura do `/webhook/evolution` (`X-Hub-Signature-256` ou `X-Evolution-Token`) |
| `EVOLUTION_URL`, `EVOLUTION_API_KEY` | Evolution (histórico e "digitando"); com `record_only` nada é enviado |
| `OMNIROUTE_API_KEY` | OmniRoute (resumos, motor legacy) |
| `REPLY_ENGINE=agent`, `REPLY_AGENT_MODEL=openai:gpt-pool` | motor de resposta = Pydantic AI pelo combo `gpt-pool` do OmniRoute (o mesmo da produção, TRAPS #68 do Next) |
| `OPENAI_BASE_URL=https://omni.geniuslabs.pro/v1`, `OPENAI_API_KEY` | como o Pydantic AI alcança o OmniRoute |
| `OUTPUT_GUARD_ENABLED=true` | guarda de saída local (credencial, texto invisível) |
| `LOGFIRE_TOKEN` | envio para o Logfire |

**Não definidas de propósito:** `DELIVERY_MODE` (padrão `record_only`: a resposta é gravada no `outbox` e **nada vai ao WhatsApp**), `DELIVERY_ALLOWLIST` (vazia: o "digitando" também não sai), `OPS_ALERT_TO`, `HANDOFF_AUTO_RELEASE_TENANTS` (vazia: o aviso no boot é esperado enquanto não houver tenant real).

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

## O que já deu errado e por quê

| Sintoma | Causa | Correção |
|---|---|---|
| `401` em todo webhook | Teste na URL da VPS (`genius-next.geniuslabs.pro`), não do Render; e `tenant_registry` vazio | URL do Render; cadastrar a instância |
| `503`, nada gravado | `clients_status_check` do Neon aceitava `new/off` em vez de `open/block` | Constraint igual ao baseline (Next TRAPS #82) |
| Turno `failed`: `DEEPSEEK_API_KEY` | Motor `agent` com o modelo DeepSeek padrão, sem a chave | `REPLY_AGENT_MODEL=openai:gpt-pool` + `OPENAI_*` |
| Toda resposta virava "Vou confirmar isso com a equipe" | Guarda de saída dependia do sidecar llm-guard, inexistente no Render | Guarda local + `OutputGuardrail` (PR #1; Next TRAPS #78) |
| Motor `legacy` respondia `action: "function"` | `gpt-oss` não segue o envelope JSON do legacy | Usar o motor `agent` |

## Pendente antes de atender cliente real

1. Trocar as chaves que circularam em 02/nov (`HMAC_SECRET`, senha do Neon, OmniRoute, Evolution, `LOGFIRE_TOKEN`).
2. Embeddings: a busca de lições e o status audit chamam um sidecar de embedding local que não existe no Render (não derruba o turno, só não tem esse contexto).
3. Plano pago (o free dorme), `DELIVERY_MODE=live` + `DELIVERY_ALLOWLIST`, `HANDOFF_AUTO_RELEASE_TENANTS` com o tenant, e cadastro pelo runbook de ativação do Next (`docs/runbooks/RUNBOOK-ATIVACAO-TENANT.md`).

## Wake do worker (Actions)

O Procrastinate roda dentro do serviço web: no plano free ele dorme com o Render (15 min sem tráfego de entrada) e leva os relógios junto. Ao acordar, os jobs `todo` vencidos rodam todos, o `reenfileira_travados` devolve à fila o que ficou `doing`, e cada relógio recupera só o **último** tick perdido (`periodic_defaults.max_delay` = 3 h). Falta só alguém acordar o processo.

`.github/workflows/wake.yml` (a cada 20 min, minutos 7/27/47) roda `scripts/wake_check.py`: consulta o Neon com um usuário **somente leitura** (`app/wake.py`: job vencido, job travado, cliente sem resposta) e, só se houver trabalho, chama `POST /v1/control/wake` assinado (mesma assinatura de `/v1/control`, segredo `CONTROL_HMAC_SECRET`). Sem trabalho, o serviço continua dormindo. A regra de quem abordar continua só em `app/durable/pendentes.py`; a contagem do wake é grossa de propósito (falso positivo custa um despertar).

| Onde | Nome | O quê |
|---|---|---|
| Segredo do GitHub | `WAKE_DATABASE_URL` | URL do Neon com role somente leitura |
| Segredo do GitHub | `CONTROL_HMAC_SECRET` | o mesmo valor definido no Render |
| Variável do Render | `CONTROL_HMAC_SECRET` | habilita `/v1/control/wake` (sem ele responde 503) |
| Variável do GitHub (opcional) | `WAKE_TENANTS` | tenants separados por vírgula; vazio = todos |

Limites: o agendamento do Actions é *best effort* (atrasa, às vezes perde). Serve para follow-ups de 30 min a 48 h; não para relógio de minuto em minuto. Teste manual: *Actions → Wake do worker → Run workflow* com `force`, ou `python scripts/wake_check.py --check-only` (só mede).
