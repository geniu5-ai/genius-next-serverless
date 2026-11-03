# Standby

App de **contingência** do Genius Next: FastAPI + Procrastinate, rodando no Render com banco no
Neon. É a alternativa pronta para subir se o agente principal (`genius-next.service`, VPS 002)
cair. Hoje roda em `record_only`: a resposta é gravada no `outbox` e **nada vai ao WhatsApp**.

## Documentação

| Documento | Para quê |
|---|---|
| [`docs/OPERACAO.md`](docs/OPERACAO.md) | Onde está, variáveis, credenciais, como testar, histórico de problemas, pendências |
| [`CLAUDE.md`](CLAUDE.md) | Regras para o Claude Code neste repositório (CEO técnico, autonomia, human-gate, como chamar a crew) |
| [`docs/GENIUS-DEV-AGENTS.md`](docs/GENIUS-DEV-AGENTS.md) | Cópia de referência das regras da casa do Genius Dev (fonte: VPS 003) |
| [`docs/HANDOFF.md`](docs/HANDOFF.md) | Estado atual e por onde começar uma sessão fria (mesmo padrão do Next) |
| [`docs/COFRE-IDEIAS.md`](docs/COFRE-IDEIAS.md) | As seis ideias do cofre, decisões e alternativas avaliadas |
| [`docs/TRAPS.md`](docs/TRAPS.md) | Armadilhas já encontradas: sintoma, causa, correção e regra |
| [`docs/WAKE.md`](docs/WAKE.md) | Por que o worker dorme no Render free, como o Actions o acorda, decisões e alternativas avaliadas |

## Atalhos

- Testar o webhook: `scripts/testar_webhook.sh`
- Medir se há trabalho vencido (só leitura): `python scripts/wake_check.py --check-only`
- Cofre de segredos: `python scripts/vault.py list` (ver `docs/OPERACAO.md`)
- Testes: `pytest -q tests`
- Workflows: `monitor.yml` (saúde, a cada 5 min), `wake.yml` (acorda o worker, a cada 20 min),
  `neon-preview-deploy.yml` (branch Neon por PR)

> O repositório e o serviço no Render se chamam **Standby**. O subdomínio do Render
> (`genius-next-serverless.onrender.com`) vem do nome antigo e não pode ser alterado num serviço
> existente; veja `docs/OPERACAO.md`.
