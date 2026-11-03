# Documentação do Standby

App de **contingência** do Genius Next (FastAPI + Procrastinate, Render + Neon). Mesmo padrão do
`HANDOFF.md` do Next: o que está no ar, o que mudou, e por onde começar uma sessão fria. Onde um
bloco de estado mais novo divergir de um texto mais antigo, vale o bloco.

| Documento | Para quê |
|---|---|
| [`../README.md`](../README.md) | porta de entrada e atalhos |
| [`../CLAUDE.md`](../CLAUDE.md) | regras para o Claude Code aqui (CEO técnico, autonomia, human-gate) |
| [`OPERACAO.md`](OPERACAO.md) | onde está, variáveis, segredos, cofre, roles, como testar, pendências |
| [`WAKE.md`](WAKE.md) | por que o worker dorme no Render free e como o Actions o acorda |
| [`TRAPS.md`](TRAPS.md) | armadilhas já encontradas (sintoma, causa, correção, regra) |
| [`GENIUS-DEV-AGENTS.md`](GENIUS-DEV-AGENTS.md) | cópia das regras da casa do Genius Dev (fonte: VPS 003) |

> **🧭 Estado em 03/nov/2026 (leia primeiro):**
> - **No ar:** serviço `standby` no Render (plano free, dorme após ~15 min), banco no Neon (projeto `Next`, `aws-sa-east-1`). `record_only`: nada vai ao WhatsApp. URL `https://genius-next-serverless.onrender.com` (o subdomínio não muda, `TRAPS` #12).
> - **Wake:** `wake.yml` a cada 20 min consulta o Neon com a role `wake_ro` e só chama `POST /v1/control/wake` quando há trabalho vencido. Verificado de ponta a ponta em 03/nov (`WAKE.md`).
> - **Cofre de segredos:** schema `standby_vault` no Neon; valores cifrados fora do banco (AES-256-GCM, nome como AAD). Acesso só por funções que registram cada leitura; histórico e log são somente de inserção; backup cifrado com `export`. `python scripts/vault.py` (`OPERACAO.md`, "Cofre"). Entrada: `STANDBY_VAULT_URL` e `STANDBY_VAULT_KEY` no ambiente da sessão.
> - **Roles do Neon criadas por SQL**, nunca pela API (`TRAPS` #8): `wake_ro` (lê 5 tabelas) e `vault_rw` (só executa as funções do cofre).
> - **Rename:** o app era `genius-next-serverless`, agora é Standby (repositório `geniu5-ai/standby`, serviço `standby`). O Genius Dev (VPS 003, DBOS) e os docs do Next citam o nome antigo; o GitHub mantém o redirecionamento.
> - **Abertos:** (1) cadastrar `STANDBY_VAULT_URL`/`STANDBY_VAULT_KEY` no ambiente da sessão (o chairman estava no celular; os valores estão em `vault-env.local`, ignorado pelo git, **não apague antes de ele copiar**, `TRAPS` #10); (2) rotacionar `wake_ro`, `CONTROL_HMAC_SECRET` e o que apareceu em conversa; (3) paridade com o Next (status, lessons, embeds, juiz); (4) failover automático; (5) job `conversation_turn` com `failed` de 02/nov 05:49 UTC ainda não analisado.

## Sessão fria: por onde começar

```bash
curl -sS https://genius-next-serverless.onrender.com/health          # {"status":"ok"} (pode levar ~60 s)
pytest -q tests                                                      # suíte sem rede e sem banco
python scripts/vault.py list                                         # precisa do ambiente do cofre
python scripts/wake_check.py --check-only                            # mede, não acorda (WAKE_DATABASE_URL)
```

Antes de mexer: leia o bloco de estado acima e as últimas entradas de [`TRAPS.md`](TRAPS.md). Ao
terminar: o que mudou fica escrito no lugar certo, na mesma entrega.
