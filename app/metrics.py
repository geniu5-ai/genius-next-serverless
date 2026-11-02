"""Prometheus /metrics export — Genius Observe phase 1.

Exposes a read-only ``GET /metrics`` in the Prometheus text format so any
scraper (Prometheus, SigNoz self-hosted — doc PG #951 — or a plain ``curl``)
can observe Genius Next. No dashboard and no alerting yet: this endpoint is
the contract, the consumers come later.

Metrics:
- ``genius_next_messages_total{tenant_id, direction}`` — persisted events per
  tenant and direction (inbound/outbound), refreshed at scrape time.
- ``genius_next_outbox_total{status}`` — outbox rows per delivery status
  (sent / failed / uncertain / recorded / pending / claimed).
- ``genius_next_webhook_latency_seconds`` — histogram of webhook handling
  time, observed in ``app.main`` (approximate, in-process).
- ``genius_next_jobs_status_total{task_name, status}`` — Procrastinate jobs
  grouped by task and status (``procrastinate_jobs``).

DB-backed values are Gauges refreshed on every scrape with short, indexed
queries; a scrape failure logs and serves the last good values instead of
taking the endpoint down.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from prometheus_client import Gauge, Histogram, generate_latest
from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.orm import Session

from app.settings import get_settings

logger = logging.getLogger(__name__)


MESSAGES = Gauge(
    "genius_next_messages_total",
    "Persisted messages per tenant and direction.",
    ("tenant_id", "direction"),
)
OUTBOX = Gauge(
    "genius_next_outbox_total",
    "Outbox rows per delivery status.",
    ("status",),
)
JOBS = Gauge(
    "genius_next_jobs_status_total",
    "Procrastinate jobs per task and status (succeeded/failed are kept 7 days).",
    ("task_name", "status"),
)
FOLLOW_UP_LIFECYCLES = Gauge(
    "genius_next_follow_up_lifecycles_total",
    "Follow-up V2 lifecycles per durable state.",
    ("state",),
)
FOLLOW_UP_RECEIPTS = Gauge(
    "genius_next_follow_up_receipts_total",
    "Follow-up V2 attempt receipts per attempt, status, and delivery mode.",
    ("attempt", "status", "delivery_mode"),
)
FOLLOW_UP_CANDIDATES = Gauge(
    "genius_next_follow_up_candidates_total",
    "Follow-up loss candidates per intake status.",
    ("status",),
)
RESCUE_CASES = Gauge(
    "genius_next_rescue_cases_total",
    "Record-only Rescue cases per status and delivery mode.",
    ("status", "delivery_mode"),
)
WEBHOOK_LATENCY = Histogram(
    "genius_next_webhook_latency_seconds",
    "Webhook processing latency in seconds (approximate, in-process).",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

_MESSAGES_SQL = text(
    """
    /* metrics.messages */
    SELECT tenant_id, direction, count(*) AS n
    FROM genius_next.events
    GROUP BY tenant_id, direction
    """
)

_OUTBOX_SQL = text(
    """
    /* metrics.outbox */
    SELECT delivery_status AS status, count(*) AS n
    FROM genius_next.outbox
    GROUP BY delivery_status
    """
)

_JOBS_SQL = text(
    """
    /* metrics.jobs */
    SELECT task_name, status::text AS status, count(*) AS n
    FROM procrastinate_jobs
    GROUP BY task_name, status
    """
)

_FOLLOW_UP_LIFECYCLES_SQL = text(
    """
    /* metrics.follow_up_lifecycles */
    SELECT state, count(*) AS n
    FROM genius_next.follow_up_lifecycles
    GROUP BY state
    """
)

_FOLLOW_UP_RECEIPTS_SQL = text(
    """
    /* metrics.follow_up_receipts */
    SELECT
        attempt,
        status,
        COALESCE(NULLIF(delivery_mode, ''), 'unknown') AS delivery_mode,
        count(*) AS n
    FROM genius_next.follow_up_attempt_receipts
    GROUP BY attempt, status, COALESCE(NULLIF(delivery_mode, ''), 'unknown')
    """
)

_FOLLOW_UP_CANDIDATES_SQL = text(
    """
    /* metrics.follow_up_candidates */
    SELECT status, count(*) AS n
    FROM genius_next.follow_up_candidates
    GROUP BY status
    """
)

_RESCUE_CASES_SQL = text(
    """
    /* metrics.rescue_cases */
    SELECT
        status,
        COALESCE(NULLIF(delivery_mode, ''), 'unknown') AS delivery_mode,
        count(*) AS n
    FROM genius_next.rescue_cases
    GROUP BY status, COALESCE(NULLIF(delivery_mode, ''), 'unknown')
    """
)


@lru_cache(maxsize=2)
def _engine(database_url: str) -> Engine:
    """Build the shared engine lazily; creating it does not open a connection."""
    url = make_url(database_url)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    return create_engine(url, pool_pre_ping=True)


def _refresh_messages(session: Session) -> None:
    rows = session.execute(_MESSAGES_SQL).mappings().all()
    MESSAGES.clear()
    for row in rows:
        MESSAGES.labels(row["tenant_id"], row["direction"]).set(int(row["n"]))


def _refresh_outbox(session: Session) -> None:
    rows = session.execute(_OUTBOX_SQL).mappings().all()
    OUTBOX.clear()
    for row in rows:
        OUTBOX.labels(row["status"]).set(int(row["n"]))


def _refresh_jobs(session: Session) -> None:
    rows = session.execute(_JOBS_SQL).mappings().all()
    JOBS.clear()
    for row in rows:
        JOBS.labels(row["task_name"], row["status"]).set(int(row["n"]))


def _refresh_commercial_flow(session: Session) -> None:
    """Refresh aggregate commercial-flow gauges without exposing customer data."""
    FOLLOW_UP_LIFECYCLES.clear()
    for row in session.execute(_FOLLOW_UP_LIFECYCLES_SQL).mappings():
        FOLLOW_UP_LIFECYCLES.labels(row["state"]).set(int(row["n"]))

    FOLLOW_UP_RECEIPTS.clear()
    for row in session.execute(_FOLLOW_UP_RECEIPTS_SQL).mappings():
        FOLLOW_UP_RECEIPTS.labels(
            str(row["attempt"]), row["status"], row["delivery_mode"]
        ).set(int(row["n"]))

    FOLLOW_UP_CANDIDATES.clear()
    for row in session.execute(_FOLLOW_UP_CANDIDATES_SQL).mappings():
        FOLLOW_UP_CANDIDATES.labels(row["status"]).set(int(row["n"]))

    RESCUE_CASES.clear()
    for row in session.execute(_RESCUE_CASES_SQL).mappings():
        RESCUE_CASES.labels(row["status"], row["delivery_mode"]).set(int(row["n"]))


def refresh_metrics(database_url: str) -> None:
    """Refresh DB-backed gauges; a failed scrape logs and keeps last values."""
    try:
        with Session(_engine(database_url)) as session:
            _refresh_messages(session)
            _refresh_outbox(session)
            _refresh_commercial_flow(session)
            _refresh_jobs(session)
    except Exception:
        logger.warning("metrics: refresh failed", exc_info=True)


def render_metrics() -> bytes:
    """Refresh DB-backed gauges and render the full Prometheus exposition."""
    refresh_metrics(get_settings().DATABASE_URL)
    return generate_latest()
