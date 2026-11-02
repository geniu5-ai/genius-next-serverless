"""Resumo do historico de um cliente antigo, disparado no primeiro contato (flow.ingest_event_workflow).

O relogio de 15 min que varria quem faltava foi apagado em 31/out/2026: 1 resumo util em 14 dias."""

from __future__ import annotations

import logging

from app.durable.persistence import record_client_summary_txn
from app.jobs import AUX_QUEUE, jobs
from app.agent.history_summary import summarize_history_step

logger = logging.getLogger(__name__)


def backfill_one_client_workflow(
    tenant_id: str,
    phone: str,
    instance: str,
    remote_jid: str,
) -> None:
    summary: str | None = None
    try:
        summary = summarize_history_step(tenant_id, phone, instance, remote_jid)
    except Exception:
        logger.warning(
            "summary backfill failed for %s/%s",
            tenant_id,
            phone,
            exc_info=True,
        )
    try:
        record_client_summary_txn(tenant_id, phone, summary)
    except Exception:
        logger.warning(
            "summary backfill record failed for %s/%s",
            tenant_id,
            phone,
            exc_info=True,
        )


@jobs.task(name="summary_backfill_one", queue=AUX_QUEUE)
def backfill_one_client_job(tenant_id: str, phone: str, instance: str, remote_jid: str) -> None:
    backfill_one_client_workflow(tenant_id, phone, instance, remote_jid)
