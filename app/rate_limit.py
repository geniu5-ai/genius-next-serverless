"""Durable token-bucket rate limiting for the Evolution webhook.

Genius Shield fase 1 (docs/roadmap/FEAT-SECURITY.md).  Each authenticated
``(tenant_id, phone)`` owns a token bucket in ``genius_next.rate_limit_buckets``.
Capacity and refill rate come from settings (never hardcoded).  The refill +
consume run in a single short transaction that takes a row lock
(``SELECT ... FOR UPDATE``), so concurrent floods cannot over-draw a bucket.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from sqlalchemy import Engine, create_engine, make_url, text

from app.settings import Settings

logger = logging.getLogger(__name__)

_FIRST_TOUCH_SQL = text(
    """
    INSERT INTO genius_next.rate_limit_buckets (
        tenant_id, phone, tokens, last_refill, updated_at
    )
    VALUES (:tenant_id, :phone, :capacity, now(), now())
    ON CONFLICT (tenant_id, phone) DO NOTHING
    """
)

_LOCK_AND_REFILL_SQL = text(
    """
    SELECT
        tokens,
        last_refill,
        LEAST(
            :capacity,
            tokens + EXTRACT(EPOCH FROM (now() - last_refill)) * :refill_per_sec
        ) AS refilled
    FROM genius_next.rate_limit_buckets
    WHERE tenant_id = :tenant_id AND phone = :phone
    FOR UPDATE
    """
)

_PERSIST_SQL = text(
    """
    UPDATE genius_next.rate_limit_buckets
    SET tokens = :tokens,
        last_refill = now(),
        updated_at = now()
    WHERE tenant_id = :tenant_id AND phone = :phone
    """
)


@lru_cache(maxsize=1)
def get_rate_limit_engine(database_url: str) -> Engine:
    """Build the shared engine lazily; creating it does not open a connection."""
    url = make_url(database_url)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    return create_engine(url, pool_pre_ping=True)


def rate_limit_allow(*, tenant_id: str, phone: str, settings: Settings) -> bool:
    """Atomically consume one token for ``(tenant_id, phone)``.

    Returns True when the request is within the configured limit (or rate
    limiting is disabled), False when the request must be silently dropped.
    """
    if not settings.RATE_LIMIT_ENABLED:
        return True

    engine = get_rate_limit_engine(settings.DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(
            _FIRST_TOUCH_SQL,
            {
                "tenant_id": tenant_id,
                "phone": phone,
                "capacity": settings.RATE_LIMIT_CAPACITY,
            },
        )
        row = connection.execute(
            _LOCK_AND_REFILL_SQL,
            {
                "tenant_id": tenant_id,
                "phone": phone,
                "capacity": settings.RATE_LIMIT_CAPACITY,
                "refill_per_sec": settings.RATE_LIMIT_REFILL_PER_SECOND,
            },
        ).one()

        refilled = float(row.refilled)
        allowed = refilled >= 1.0
        connection.execute(
            _PERSIST_SQL,
            {
                "tenant_id": tenant_id,
                "phone": phone,
                "tokens": refilled - 1 if allowed else refilled,
            },
        )

    if not allowed:
        logger.warning(
            "rate limit exceeded tenant_id=%s phone=%s",
            tenant_id,
            phone,
        )
    return allowed
