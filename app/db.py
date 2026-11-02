"""Transacao e sessao do Next (31/out/2026).

Uma funcao decorada com :func:`transaction` roda numa transacao propria (SERIALIZABLE
por padrao) e enxerga a sessao por :func:`sql_session`. Conflito de serializacao
(SQLSTATE 40001) e deadlock (40P01) repetem a funcao inteira, com espera curta e
aleatoria.

Nao existe ledger de resultado de transacao: a idempotencia do Next vem das chaves
unicas do proprio banco (evento, outbox, tentativa de follow-up).

Regras:
- ``sql_session()`` fora de uma transacao levanta ``RuntimeError`` (era a TRAPS #51 e #60).
- Transacao dentro de transacao reaproveita a de fora.
- O corpo da funcao pode ser repetido: nada de efeito externo (HTTP, envio) dentro dele.
"""

from __future__ import annotations

import functools
import logging
import random
import time
from collections.abc import Callable
from contextvars import ContextVar
from functools import lru_cache
from typing import Any, TypeVar, overload

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.settings import get_settings

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

_SESSION: ContextVar[Session | None] = ContextVar("genius_next_sql_session", default=None)
_RETRYABLE = {"40001", "40P01"}
# Conflito de serializacao e esperado em linha quente (turno + auditoria + follow-up no mesmo
# cliente): espera exponencial com jitter, ate ~8 s no pior caso, antes de desistir.
_MAX_ATTEMPTS = 12
_BACKOFF_BASE_S = 0.02
_BACKOFF_CAP_S = 1.0


@lru_cache(maxsize=4)
def get_engine(database_url: str) -> Engine:
    """Engine compartilhada; criar nao abre conexao."""
    url = make_url(database_url)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10)


def sql_session() -> Session:
    session = _SESSION.get()
    if session is None:
        raise RuntimeError("sql_session() so existe dentro de uma funcao @transaction")
    return session


def _sqlstate(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


def _run_once(fn: Callable[..., Any], isolation: str, args: tuple, kwargs: dict) -> Any:
    session = Session(get_engine(get_settings().DATABASE_URL), expire_on_commit=False)
    token = _SESSION.set(session)
    try:
        session.connection(execution_options={"isolation_level": isolation})
        result = fn(*args, **kwargs)
        session.commit()
        return result
    except BaseException:
        session.rollback()
        raise
    finally:
        _SESSION.reset(token)
        session.close()


@overload
def transaction(fn: F) -> F: ...
@overload
def transaction(*, isolation_level: str = "SERIALIZABLE") -> Callable[[F], F]: ...


def transaction(fn: Callable[..., Any] | None = None, *, isolation_level: str = "SERIALIZABLE"):
    """``@transaction`` ou ``@transaction(isolation_level="READ COMMITTED")``."""

    def decorate(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if _SESSION.get() is not None:
                return func(*args, **kwargs)  # ja dentro de uma transacao: reaproveita
            for attempt in range(1, _MAX_ATTEMPTS + 1):
                try:
                    return _run_once(func, isolation_level, args, kwargs)
                except DBAPIError as exc:
                    if _sqlstate(exc) not in _RETRYABLE or attempt == _MAX_ATTEMPTS:
                        raise
                    logger.info("%s: conflito %s, tentativa %d", func.__name__, _sqlstate(exc), attempt)
                    time.sleep(random.uniform(0, min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * 2**attempt)))
            raise AssertionError("inalcancavel")

        return wrapper  # type: ignore[return-value]

    return decorate(fn) if fn is not None else decorate
