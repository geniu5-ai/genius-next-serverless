"""Verificação pontual do módulo de status — roda antes de qualquer alteração.

Quatro camadas, em ordem de custo crescente:
  L1  Regex determinístico (rules.py) — puro, zero I/O
  L2  Contratos Pydantic (audit.py)   — puro, zero I/O
  L3  Embed service (localhost:8721)  — HTTP local
  L4  Banco de dados                  — query SQL direta

Uso:
    python app/status/verify_scripts.py          # todas as camadas
    python app/status/verify_scripts.py --l1 --l2  # só puras

Saída: tabela + exit 0 se tudo verde, exit 1 se qualquer check falhou.
O agente que chama este script lê o exit code e a tabela antes de editar.
"""

from __future__ import annotations

import argparse
import os
import sys
import textwrap
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Resultado estruturado
# ---------------------------------------------------------------------------

type _Check = tuple[str, bool, str]  # (label, ok, detail)


def _ok(label: str, detail: str = "") -> _Check:
    return label, True, detail


def _fail(label: str, detail: str) -> _Check:
    return label, False, detail


# ---------------------------------------------------------------------------
# L1 — Regex determinístico (rules.py)
# ---------------------------------------------------------------------------

def _l1_checks() -> list[_Check]:
    from app.status.rules import detect_status_by_rule, is_trivial_message, is_trivial_no_reply

    results: list[_Check] = []

    # Casos que DEVEM retornar "win"
    win_cases = [
        "quero marcar pra hoje",
        "quero marcar pra quinta-feira",
        "confirmo o horário",
        "aceito a proposta",
        "quero comprar",
        "quero fechar o contrato",
    ]
    for text in win_cases:
        got = detect_status_by_rule(text)
        if got == "win":
            results.append(_ok(f"L1/win: {text[:40]!r}"))
        else:
            results.append(_fail(f"L1/win: {text[:40]!r}", f"esperado=win got={got!r}"))

    # Casos que DEVEM retornar "loss"
    loss_cases = [
        "não tenho interesse",
        "não vou fechar não",
        "desisti",
        "não me manda mais mensagem",
        "me remove da lista",
        "para de mandar mensagem",
    ]
    for text in loss_cases:
        got = detect_status_by_rule(text)
        if got == "loss":
            results.append(_ok(f"L1/loss: {text[:40]!r}"))
        else:
            results.append(_fail(f"L1/loss: {text[:40]!r}", f"esperado=loss got={got!r}"))

    # Casos ambíguos (win+loss no mesmo texto) → DEVEM retornar None
    # Trap clássica: sinal de win antes do gatilho de recusa marcava loss.
    ambig_cases = [
        ("Podemos marcar pra hoje? Não quero mais esperar.", None),
        ("Não tenho interesse em consulta avulsa, quero fechar o pacote.", None),
        ("Quero fechar, mas só se tiver vaga amanhã.", None),
    ]
    for text, expected in ambig_cases:
        got = detect_status_by_rule(text)
        if got == expected:
            results.append(_ok(f"L1/ambig: {text[:45]!r}"))
        else:
            results.append(_fail(f"L1/ambig: {text[:45]!r}", f"esperado={expected!r} got={got!r}"))

    # Casos que DEVEM retornar None (sem sinal)
    none_cases = ["vou pensar", "depois eu vejo", "quanto custa?", "oi tudo bem"]
    for text in none_cases:
        got = detect_status_by_rule(text)
        if got is None:
            results.append(_ok(f"L1/none: {text[:40]!r}"))
        else:
            results.append(_fail(f"L1/none: {text[:40]!r}", f"esperado=None got={got!r}"))

    # is_trivial_message
    trivial_status = ["obrigada!!!", "kkkkkk", "👍", "rsrsrs"]
    non_trivial_status = ["quero marcar", "quanto custa?", "ok", "sim"]
    for text in trivial_status:
        if is_trivial_message(text):
            results.append(_ok(f"L1/trivial-status: {text!r}"))
        else:
            results.append(_fail(f"L1/trivial-status: {text!r}", "deveria ser trivial para status"))
    for text in non_trivial_status:
        if not is_trivial_message(text):
            results.append(_ok(f"L1/não-trivial-status: {text!r}"))
        else:
            results.append(_fail(f"L1/não-trivial-status: {text!r}", "não deveria ser trivial para status"))

    # is_trivial_no_reply
    trivial_reply = ["ok", "blz", "beleza", "certo", "aham", "show"]
    non_trivial_reply = ["sim", "não", "ok, mas quanto custa?"]
    for text in trivial_reply:
        if is_trivial_no_reply(text):
            results.append(_ok(f"L1/trivial-reply: {text!r}"))
        else:
            results.append(_fail(f"L1/trivial-reply: {text!r}", "deveria suprimir resposta"))
    for text in non_trivial_reply:
        if not is_trivial_no_reply(text):
            results.append(_ok(f"L1/não-trivial-reply: {text!r}"))
        else:
            results.append(_fail(f"L1/não-trivial-reply: {text!r}", "não deveria suprimir resposta"))

    return results


# ---------------------------------------------------------------------------
# L2 — Contratos Pydantic (audit.py)
# ---------------------------------------------------------------------------

def _l2_checks() -> list[_Check]:
    from pydantic import ValidationError
    from app.status.audit import StatusInput, StatusResult, build_status_context

    results: list[_Check] = []

    # StatusInput aceita papéis válidos
    try:
        inp = StatusInput(
            history=[("customer", "oi"), ("assistant", "olá")],
            current_lead="quero marcar pra hoje",
            agent_reply="Confirmado!",
        )
        results.append(_ok("L2/StatusInput válido"))
    except Exception as exc:
        results.append(_fail("L2/StatusInput válido", str(exc)))

    # StatusInput rejeita papel desconhecido
    try:
        StatusInput(history=[("system", "x")], current_lead="x", agent_reply="x")
        results.append(_fail("L2/StatusInput rejeita role inválido", "não levantou ValidationError"))
    except (ValidationError, ValueError):
        results.append(_ok("L2/StatusInput rejeita role inválido"))

    # StatusResult default é open
    try:
        r = StatusResult()
        assert r.customer_status == "open"
        assert r.confidence == 0.0
        results.append(_ok("L2/StatusResult default open"))
    except Exception as exc:
        results.append(_fail("L2/StatusResult default open", str(exc)))

    # Status terminal exige evidence_source="customer"
    try:
        StatusResult(customer_status="loss", confidence=0.9, evidence_source="agent_reply")
        results.append(_fail("L2/terminal exige evidence_source=customer", "não levantou"))
    except (ValidationError, ValueError):
        results.append(_ok("L2/terminal exige evidence_source=customer"))

    # build_status_context nunca mistura reply na fala do cliente
    try:
        inp = StatusInput(
            history=[("customer", "oi"), ("assistant", "olá")],
            current_lead="quero marcar",
            agent_reply="Marcado para amanhã!",
        )
        ctx = build_status_context(inp)
        customer_texts = [item["text"] for item in ctx["turn"]]
        assert "Marcado para amanhã!" not in customer_texts, "reply vazou para turn do cliente"
        assert ctx["agent_reply"] == "Marcado para amanhã!"
        results.append(_ok("L2/reply não vaza para turn do cliente"))
    except Exception as exc:
        results.append(_fail("L2/reply não vaza para turn do cliente", str(exc)))

    return results


# ---------------------------------------------------------------------------
# L3 — Embed service
# ---------------------------------------------------------------------------

def _l3_checks() -> list[_Check]:
    from app.status.audit import EMBED_SCORE_URL, EMBED_SCORE_THRESHOLD

    results: list[_Check] = []

    # Probe de conectividade
    payload = '{"text": "quero fechar", "labels": ["win", "loss"]}'.encode()
    req = urllib.request.Request(
        EMBED_SCORE_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        t0 = time.monotonic()
        with urllib.request.urlopen(req, timeout=3) as resp:
            latency_ms = int((time.monotonic() - t0) * 1000)
            body = resp.read().decode()
        results.append(_ok("L3/embed service online", f"{latency_ms}ms"))
    except urllib.error.URLError as exc:
        results.append(_fail("L3/embed service online", str(exc)))
        return results  # sem serviço, pula checks de score

    # Score de win claro deve superar threshold
    import json

    def _score(text: str, label: str) -> float:
        data = json.dumps({"text": text, "labels": [label]}).encode()
        r = urllib.request.Request(
            EMBED_SCORE_URL, data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(r, timeout=3) as resp:
            return json.loads(resp.read())["scores"][label]

    try:
        score_win = _score("quero fechar o contrato", "win")
        if score_win >= EMBED_SCORE_THRESHOLD:
            results.append(_ok("L3/score win claro", f"{score_win:.3f} >= {EMBED_SCORE_THRESHOLD}"))
        else:
            results.append(_fail("L3/score win claro", f"{score_win:.3f} < threshold {EMBED_SCORE_THRESHOLD}"))
    except Exception as exc:
        results.append(_fail("L3/score win claro", str(exc)))

    try:
        score_loss = _score("não tenho interesse nenhum", "loss")
        if score_loss >= EMBED_SCORE_THRESHOLD:
            results.append(_ok("L3/score loss claro", f"{score_loss:.3f} >= {EMBED_SCORE_THRESHOLD}"))
        else:
            results.append(_fail("L3/score loss claro", f"{score_loss:.3f} < threshold {EMBED_SCORE_THRESHOLD}"))
    except Exception as exc:
        results.append(_fail("L3/score loss claro", str(exc)))

    # Score de texto neutro deve FICAR ABAIXO do threshold (evitar falso positivo)
    try:
        score_neutral_win = _score("oi tudo bem", "win")
        if score_neutral_win < EMBED_SCORE_THRESHOLD:
            results.append(_ok("L3/neutro não dispara win", f"{score_neutral_win:.3f}"))
        else:
            results.append(_fail("L3/neutro não dispara win", f"{score_neutral_win:.3f} >= threshold — falso positivo"))
    except Exception as exc:
        results.append(_fail("L3/neutro não dispara win", str(exc)))

    return results


# ---------------------------------------------------------------------------
# L4 — Banco de dados (query direta)
# ---------------------------------------------------------------------------

def _l4_checks() -> list[_Check]:
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        return [_fail("L4/DATABASE_URL", "não definida — pule com --skip-db")]

    results: list[_Check] = []

    try:
        import sqlalchemy as sa
        engine = sa.create_engine(db_url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
        with engine.connect() as conn:
            results.append(_ok("L4/conexão DB"))

            # Tabela status_audits existe
            row = conn.execute(sa.text(
                "SELECT to_regclass('genius_next.status_audits') IS NOT NULL"
            )).scalar()
            if row:
                results.append(_ok("L4/tabela status_audits existe"))
            else:
                results.append(_fail("L4/tabela status_audits existe", "tabela não encontrada"))

            # Tabela clients tem coluna status com tipo text
            row = conn.execute(sa.text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema='genius_next' AND table_name='clients' AND column_name='status'"
            )).scalar()
            if row:
                results.append(_ok("L4/clients.status existe", f"type={row}"))
            else:
                results.append(_fail("L4/clients.status existe", "coluna não encontrada"))

            # Trap #9: clientes em crew com crew=False ficam presos para sempre.
            # Detecta quantos estão nesse estado — qualquer número > 0 é alerta.
            count = conn.execute(sa.text(
                "SELECT count(*) FROM genius_next.clients "
                "WHERE automation_state = 'crew' "
                "  AND (metadata->>'crew')::boolean IS NOT TRUE"
            )).scalar()
            if count == 0:
                results.append(_ok("L4/trap#9 crew sem operador", "0 clientes presos"))
            else:
                results.append(_fail("L4/trap#9 crew sem operador", f"{count} cliente(s) presos em crew sem crew=true"))

            # Trap #11: duplicatas em tenant_prompt_profiles fazem modelo receber prompt errado.
            dupes = conn.execute(sa.text(
                "SELECT count(*) FROM ("
                "  SELECT tenant_id, count(*) c FROM genius_next.tenant_prompt_profiles "
                "  WHERE active = true GROUP BY tenant_id HAVING count(*) > 1"
                ") x"
            )).scalar()
            if dupes == 0:
                results.append(_ok("L4/trap#11 sem prompt duplicado"))
            else:
                results.append(_fail("L4/trap#11 sem prompt duplicado", f"{dupes} tenant(s) com perfis ativos duplicados"))

            # Auditoria de erros recentes no status_audit_log (últimos 30min)
            try:
                errors = conn.execute(sa.text(
                    "SELECT count(*) FROM genius_next.status_audit_log "
                    "WHERE created_at > now() - interval '30 minutes' AND error IS NOT NULL"
                )).scalar()
                if errors == 0:
                    results.append(_ok("L4/status_audit_log sem erros recentes"))
                else:
                    results.append(_fail("L4/status_audit_log sem erros recentes", f"{errors} erro(s) nos últimos 30min"))
            except Exception:
                results.append(_ok("L4/status_audit_log (tabela ausente — ok em dev)"))

    except Exception as exc:
        results.append(_fail("L4/conexão DB", str(exc)))

    return results


# ---------------------------------------------------------------------------
# Tabela de saída
# ---------------------------------------------------------------------------

_GREEN = "\033[32m"
_RED = "\033[31m"
_RESET = "\033[0m"
_BOLD = "\033[1m"


def _print_table(checks: list[_Check], use_color: bool = True) -> int:
    failures = 0
    col = max((len(label) for label, _, _ in checks), default=20) + 2

    print()
    print(f"  {'CHECK':<{col}}  {'STATUS':<8}  DETAIL")
    print(f"  {'-' * col}  {'--------'}  {'------'}")
    for label, ok, detail in checks:
        icon = "✓" if ok else "✗"
        color = (_GREEN if ok else _RED) if use_color else ""
        reset = _RESET if use_color else ""
        detail_str = textwrap.shorten(detail, 60) if detail else ""
        print(f"  {color}{icon}{reset} {label:<{col}}  {'OK' if ok else 'FAIL':<8}  {detail_str}")
        if not ok:
            failures += 1

    print()
    total = len(checks)
    passed = total - failures
    summary_color = _GREEN if failures == 0 else _RED
    reset = _RESET if use_color else ""
    print(f"  {summary_color}{_BOLD}{passed}/{total} checks passaram{reset}")
    print()
    return failures


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Verifica o módulo app/status/")
    parser.add_argument("--l1", action="store_true", help="Só camada 1 (regex)")
    parser.add_argument("--l2", action="store_true", help="Só camada 2 (pydantic)")
    parser.add_argument("--l3", action="store_true", help="Só camada 3 (embed)")
    parser.add_argument("--l4", action="store_true", help="Só camada 4 (DB)")
    parser.add_argument("--skip-db", action="store_true", help="Pula camada 4")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args()

    run_all = not any([args.l1, args.l2, args.l3, args.l4])

    checks: list[_Check] = []
    if run_all or args.l1:
        checks += _l1_checks()
    if run_all or args.l2:
        checks += _l2_checks()
    if run_all or args.l3:
        checks += _l3_checks()
    if (run_all or args.l4) and not args.skip_db:
        checks += _l4_checks()

    failures = _print_table(checks, use_color=not args.no_color)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
