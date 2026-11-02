# status — módulo de status do cliente (regra → embedding → LLM residual)

Pacote Python `app.status`, importado pelo fluxo durável (`app/durable/flow.py`).
Este README começou em 2026-08-25 como índice de uma "cópia de referência" da
VPS002 (fontes e testes em `py/`, `tests/`, `docs/`). Essas pastas não existem
mais; a estrutura abaixo foi atualizada em 21/out/2026.

## Estrutura (21/out/2026)
- `__init__.py`           -> docstring do módulo (rules, audit, workflow)
- `rules.py`              -> regex determinístico; única camada autoritativa (win/loss)
- `audit.py`              -> contratos Pydantic e cascata rule → embedding → LLM residual
- `workflow.py`           -> workflow DBOS pós-rascunho (`STATUS_AUDIT_ENABLED`, padrão `True`: `app/settings.py:99`)
- `conversation_audit.py` -> auditoria pós-hoc da conversa; `flow.py` só chama `audit_turn` (import em `app/durable/flow.py:47`, uso em `:974`)
- `verify_scripts.py`     -> verificação pontual em 4 camadas (L1 regex, L2 contratos, L3 embed service, L4 banco); `python app/status/verify_scripts.py [--l1 --l2 ...]`
- `migrations/`           -> cópias de referência: `005_status_simplify.sql` e `015_status_transition_guard.sql` (idênticas às de `migrations/` na raiz) e `020_status_audits__main.sql`
- `sql/`                  -> `020_status_audits__main.sql` (igual à de `migrations/` daqui) e `020_status_audits__worktree.sql`

As migrations que valem são as da pasta `migrations/` na raiz do repositório
(`005`, `015`, `020_status_audits.sql`, `032_status_vocabulario.sql`,
`033_permite_reopen_de_loss.sql`). A cópia `020_status_audits__main.sql` daqui
**difere** de `migrations/020_status_audits.sql` da raiz; qual está aplicada em
produção (nao verificado).

Testes vivos do módulo ficam em `tests/` na raiz: `test_status_rules.py`,
`test_status_workflow.py`, `test_status_layers.py`, `test_status_persistence.py`,
`test_status_reopen_trigger.py` (este exige Postgres de teste, ver `tests/README.md`).

## Histórico (superado em 21/out/2026)
Reunido em 2026-08-25 a partir de:
- /home/ubuntu/Genius-Next (branch main)
- /home/ubuntu/Genius-Next-status-audit (branch feat/status-audit-perfect)

main e worktree eram VERSÕES DIFERENTES, preservadas com prefixo `_main` / `_worktree`:
reply_status_audit:  main = c3d378f7... | worktree = a26feeb9...
020_status_audits:   main = 18b59a3b... | worktree = 08298cfb...

Pastas que existiam nessa cópia e não existem mais: `py/` (reply_status_audit__main/__worktree,
durable_status_audit__worktree), `tests/` (main_* / worktree_*), `docs/` (plan, progress,
design, spec v12 do worktree). Só `sql/` sobrevive (com a 020 main e worktree).
