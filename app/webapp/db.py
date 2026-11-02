"""DB helpers for webapp API endpoints (reads, plus the small write behind "hide message")."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from app.settings import get_settings


@lru_cache(maxsize=1)
def _engine() -> Engine:
    url = make_url(get_settings().DATABASE_URL)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    return create_engine(url, pool_pre_ping=True)


def fetch_all(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    with _engine().connect() as conn:
        result = conn.execute(text(sql), params or {})
        cols = list(result.keys())
        return [dict(zip(cols, row)) for row in result.fetchall()]


def execute_write(sql: str, params: dict[str, Any] | None = None) -> int:
    """Run one write statement in its own transaction; returns the affected row count."""
    with _engine().begin() as conn:
        return conn.execute(text(sql), params or {}).rowcount
