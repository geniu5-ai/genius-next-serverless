"""Resumo diario da conversa de cada lead (clients.metadata.lead_summary).

Fonte: a tabela ``events`` (qualquer provedor: Zernio ou Evolution). O resumo antigo
(``metadata.summary``, deep-fetch da Evolution no primeiro contato) nao cobre o Zernio e e
injetado no prompt da Julia; este e SEPARADO e nao entra no prompt: serve ao Manager, a equipe
e ao CRM. Roda 1x ao dia (06h BRT) e pode ser chamado a mao com ``run()``.
"""

from __future__ import annotations

import json
import logging
import httpx
from sqlalchemy import create_engine, text

from app.jobs import CRON_QUEUE, jobs
from app.settings import get_settings

logger = logging.getLogger(__name__)

_MAX_CLIENTS = 80
_MAX_EVENTS = 80
_MAX_CHARS = 7000

_SYSTEM = (
    "Voce resume conversas de WhatsApp de uma clinica/escritorio para a equipe. Escreva 3 a 6 frases "
    "em portugues, texto simples, sem markdown e sem inventar nada: o que a pessoa quer, valores e "
    "datas ja informados, objecoes, se aceitou, recusou ou ficou de decidir, e o que esta pendente "
    "com a equipe. Se so houve saudacao, diga isso em uma frase. Nunca copie telefone, CPF ou dado "
    "de pagamento."
)

_CANDIDATES_SQL = text(
    """
    SELECT c.tenant_id, c.phone
    FROM genius_next.clients c
    JOIN genius_next.tenant_registry r ON r.tenant_id = c.tenant_id AND r.enabled
    WHERE c.status <> 'block'
      AND EXISTS (
        SELECT 1 FROM genius_next.events e
        WHERE e.tenant_id = c.tenant_id AND e.phone = c.phone
          AND e.text_content IS NOT NULL
          AND e.received_at > COALESCE((c.metadata->>'lead_summary_at')::timestamptz, 'epoch'::timestamptz)
      )
      AND (
        SELECT count(*) FROM genius_next.events e2
        WHERE e2.tenant_id = c.tenant_id AND e2.phone = c.phone AND e2.text_content IS NOT NULL
      ) >= 2
      AND c.phone NOT IN (
        SELECT regexp_replace(manager_jid, '@.*$', '') FROM genius_next.tenant_registry
        WHERE manager_jid IS NOT NULL
      )
    GROUP BY c.tenant_id, c.phone
    ORDER BY max(c.last_seen) DESC NULLS LAST
    LIMIT :limit
    """
)
_EVENTS_SQL = text(
    """
    SELECT direction, text_content FROM (
      SELECT direction, text_content, received_at FROM genius_next.events
      WHERE tenant_id = :t AND phone = :p AND text_content IS NOT NULL AND direction IN ('inbound','outbound')
      ORDER BY received_at DESC LIMIT :n
    ) s ORDER BY received_at ASC
    """
)
_SAVE_SQL = text(
    """
    UPDATE genius_next.clients
    SET metadata = COALESCE(metadata, '{}'::jsonb)
        || jsonb_build_object('lead_summary', CAST(:s AS text), 'lead_summary_at', now())
    WHERE tenant_id = :t AND phone = :p
    """
)


def _engine():
    url = get_settings().DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return create_engine(url, pool_pre_ping=True)


def _summarize(transcript: str) -> str | None:
    s = get_settings()
    key = s.OMNIROUTE_API_KEY.get_secret_value() if s.OMNIROUTE_API_KEY else ""
    with httpx.Client(timeout=45.0) as http:
        r = http.post(
            f"{s.OMNIROUTE_BASE_URL.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": s.OMNIROUTE_MODEL,
                "temperature": 0,
                "stream": False,
                "max_tokens": 400,
                "messages": [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": transcript}],
            },
        )
    if r.status_code != 200:
        raise RuntimeError(f"resumo: LLM status {r.status_code}")
    choice = r.json()["choices"][0]
    if choice.get("finish_reason") != "stop":
        raise RuntimeError("resumo: LLM nao terminou (finish_reason)")
    return (choice["message"]["content"] or "").strip() or None


def run(*, limit: int = _MAX_CLIENTS, dry_run: bool = False) -> dict[str, int]:
    """Resume os leads com mensagem nova desde o ultimo resumo. Falha por lead nunca para o lote."""
    eng = _engine()
    ok = failed = 0
    with eng.connect() as conn:
        candidates = conn.execute(_CANDIDATES_SQL, {"limit": limit}).mappings().all()
    for cand in candidates:
        t, p = cand["tenant_id"], cand["phone"]
        try:
            with eng.connect() as conn:
                rows = conn.execute(_EVENTS_SQL, {"t": t, "p": p, "n": _MAX_EVENTS}).mappings().all()
            lines = [
                f"{'Cliente' if r['direction'] == 'inbound' else 'Atendente'}: {r['text_content'].strip()}"
                for r in rows
            ]
            transcript = "\n".join(lines)[-_MAX_CHARS:]
            summary = _summarize(transcript)
            if summary and not dry_run:
                with eng.begin() as conn:
                    conn.execute(_SAVE_SQL, {"s": summary, "t": t, "p": p})
            ok += 1
        except Exception:
            failed += 1
            logger.warning("lead_summary falhou (tenant=%s, final=%s)", t, p[-4:], exc_info=True)
    # Falha total (ex.: LLM fora do ar) nao pode passar em silencio: vira ERROR no log e o guard le.
    if candidates and ok == 0:
        logger.error("lead_summary: TODOS os %d resumos falharam", failed)
    return {"candidatos": len(candidates), "ok": ok, "falhas": failed}


@jobs.periodic(cron="0 9 * * *")
@jobs.task(name="lead_summary", queue=CRON_QUEUE)
def lead_summary_scheduled(timestamp: int) -> None:
    logger.info("lead_summary_scheduled: inicio")
    result = run()
    logger.info("lead_summary_scheduled: fim %s", json.dumps(result))
