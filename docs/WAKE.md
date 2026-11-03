# Wake do worker

Como o Standby (Procrastinate dentro de um serviço web no Render free) continua cumprindo prazos
mesmo dormindo. Estado em 03/nov/2026.

## O problema

O worker do Procrastinate roda **dentro** do processo web (`jobs_worker` no `lifespan` de
`app/main.py`). O Render free derruba o processo após ~15 min sem **tráfego de entrada** (HTTP ou
WebSocket); um job interno não conta como tráfego. Dormindo, não há worker, e a documentação do
Procrastinate é explícita: sem worker, os relógios (`@jobs.periodic`) não disparam.

O que depende do app acordado:

| Peça | Onde | Cadência |
|---|---|---|
| `reaper`, `reenfileira_travados`, `manager_tick` | `app/durable/`, `app/jobs.py` | a cada minuto |
| `pendentes_sweep` (cliente que escreveu e ficou sem resposta) | `app/durable/pendentes.py` | a cada 30 min |
| `lead_summary`, `lesson_extraction`, `limpa_jobs_antigos` | `app/agent/`, `app/jobs.py` | diário / 12 h / hora |
| Follow-ups | `app/durable/followup.py` | 5 min, 30 min, 24 h, 48 h (`schedule_in`, em `procrastinate_jobs`) |

A mensagem de um cliente acorda o app (é tráfego de entrada), então o fluxo normal funciona. O
que falta é acordar na hora de um follow-up de 30 min, 24 h ou 48 h, de uma varredura ou de um
job que ficou `doing` quando o processo caiu.

## O que o Procrastinate faz ao voltar

Verificado na [documentação](https://procrastinate.readthedocs.io/en/stable/howto/advanced/cron.html)
e em `app/jobs.py`:

- **Jobs `todo` com `scheduled_at` vencido** (follow-ups): rodam todos, porque são linhas no banco.
- **Jobs `doing` de worker morto**: o Procrastinate considera o worker morto após 30 s sem
  heartbeat (enviado a cada 10 s). O relógio `reenfileira_travados` os devolve à fila quando
  roda.
- **Relógios**: ao subir, o worker defere os ticks perdidos, mas **só o último de cada relógio** e
  só se estiver dentro de `periodic_defaults.max_delay` (3 h aqui; o padrão é 10 min). Não há
  catch-up dos N ticks atrasados. O banco garante um disparo por período
  (`UNIQUE (task_name, periodic_id, defer_timestamp)`).
- **Envio ao WhatsApp** não retenta de propósito (reenviar seria mandar duas mensagens). Se o
  processo cair no instante do envio a mensagem pode se perder; o `pendentes_sweep` é a rede de
  segurança (acha a última mensagem do cliente sem resposta, de 3 min a 23 h).
- **Idempotência**: `outbox` tem chave única `(tenant_id, idempotency_key)`; repetir um turno não
  duplica a saída no banco.

Conclusão: basta alguém acordar o processo. A regra de negócio (quem abordar, quando) continua só
no app.

## A solução: wake por Actions

```
GitHub Actions (minutos 7, 27, 47)            Render (free)                  Neon
        │                                          │                           │
        │── scripts/wake_check.py ── SELECT (role wake_ro) ───────▶│
        │       há trabalho vencido?                                           │
        │── não: termina, o serviço continua dormindo                          │
        │── sim: POST /v1/control/wake (HMAC) ─────▶ acorda (~60 s)            │
        │                                          │ Procrastinate retoma ────▶│
```

Peças:

| Arquivo | Papel |
|---|---|
| `app/wake.py` | O SQL (`WAKE_SQL`) e o `WakeState`. Sem importar o app, para servir ao script e ao serviço |
| `scripts/wake_check.py` | Mede no Neon com `psycopg` e, se preciso, chama o endpoint com retentativas (cold start). `--check-only` só mede. Carrega `app/wake.py` pelo caminho porque `import app.wake` executaria `app/__init__.py`, que importa o app inteiro |
| `POST /v1/control/wake` | Mesma assinatura de `/v1/control/*` (`X-Control-Timestamp` + `X-Control-Signature-256`, replay limitado a `CONTROL_HMAC_MAX_AGE_SECONDS`). Falha fechado (503 sem segredo, 401 com assinatura errada). Somente leitura |
| `.github/workflows/wake.yml` | Cron `7,27,47 * * * *` e `workflow_dispatch` (com `force`) |

O que conta como "trabalho vencido" (qualquer um acorda):

1. Job `todo` fora da fila `cron` com `scheduled_at` vencido ou vencendo em até 5 min
   (`WAKE_LOOKAHEAD_S`).
2. Job `doing` cujo worker sumiu ou está sem heartbeat há mais de 60 s.
3. Cliente com a última mensagem de texto sem resposta (nem bot, nem humano) entre 3 min e 23 h,
   conversa `open`. Esta contagem é **grossa de propósito**: falso positivo custa um despertar;
   falso negativo custaria um cliente sem resposta. Quem decide de fato é o `pendentes_sweep`,
   com todas as guardas.

### Limites conhecidos

- O agendamento do Actions é *best effort*: há relatos de atrasos de horas e execuções perdidas.
  Serve para follow-ups de 30 min a 48 h; **não** é um relógio de minuto em minuto.
- O monitor `monitor.yml` já faz `GET /health` a cada 5 min, o que também acorda o app. O wake
  existe para não depender disso e para só acordar com motivo.
- O Render pede, na comunidade deles, para não manter free tier acordado por ping. O wake só
  acorda quando há trabalho.
- Se o banco estiver inacessível, `wake_check.py` sai com código 2 (não acorda).

### Códigos de saída de `wake_check.py`

| Código | Significado |
|---|---|
| 0 | ok: acordou, ou não havia trabalho |
| 1 | havia trabalho e não conseguiu acordar (4 tentativas) |
| 2 | erro de uso ou de banco (variável faltando, URL inválida, banco inacessível) |

## Configuração

Ver "Segredos e credenciais" e "Wake do worker" em [`OPERACAO.md`](OPERACAO.md).

## Alternativas avaliadas (03/nov/2026)

| Opção | Veredito |
|---|---|
| Plano pago do Render (Starter, ~US$ 7/mês) | O mais simples: sem sono, relógios rodam sozinhos. Escolha se o standby precisar responder sem os ~60 s de acordar |
| **Actions como ticker (escolhida)** | Grátis; atraso de agendamento aceitável para follow-ups longos |
| QStash (Upstash) | Candidato para a segunda fase: agenda uma chamada HTTP numa hora exata. Quando o app criasse um job com `schedule_in` grande, publicaria também uma mensagem atrasada para `/v1/control/wake`, acordando no minuto certo sem varredura. Conferir o limite do plano free |
| Cloudflare Worker com cron de 1 min | Mais pontual que o Actions; as fontes divergem sobre o limite de triggers do plano free |
| Cloud Run | Com CPU limitada fora das requisições, o worker em segundo plano pararia como no Render; precisaria de instância mínima paga |
| Netlify Functions | Servem como gatilho, mas o timeout curto não hospeda o worker |
| Oracle Always Free | Host sempre ligado, mas é o mesmo provedor das VPS (falha correlacionada com a produção) |
| Actions rodando o worker em lote | Latência de minutos e dependente do agendamento; não serve para atender cliente |

## Próximos passos

1. **Failover** (não implementado): o mesmo workflow checa a saúde da VPS 002 e, só se ela estiver
   fora por N execuções seguidas, acorda o Standby e liga `DELIVERY_MODE=live`, com uma trava para
   nunca dois agentes responderem ao mesmo cliente.
2. QStash para acordar na hora exata de follow-ups longos.
3. `MANAGER_PENDENTES_TENANTS` definido, para o `pendentes_sweep` abordar quem ficou sem resposta.
