"""Operator ("Assumir") send through Zernio for POST /v1/webapp/send.

The Genius App used to send straight through Evolution, which cannot reach a
Zernio (WhatsApp Cloud API) contact.  The App now asks the Next, which knows the
Zernio ``conversationId``.  This module owns the whole decision and returns a
plain ``(status_code, body)`` so the router stays thin:

  * route:       tenant needs an ENABLED ``tenant_registry`` row with provider zernio
  * policy:      ``DELIVERY_ALLOWLIST`` (with the Brazilian ninth-digit twin) and live mode
  * idempotency: ``genius_next.webapp_sends`` keyed by (tenant_id, send_id)
  * send:        the same Zernio client/contract as the outbox (``deliver_text``)
  * record:      an outbound event shaped like the outbox reply events, so the
                 message shows up in ``GET /v1/webapp/conversations/messages``

The message text is never logged; ``actor_id`` is.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.contracts import EvolutionWebhook
from app.durable import zernio_delivery
from app.durable.outbox import destination_allowed
from app.durable.persistence import (
    _CONVERSATION_ADVANCE_SQL,
    _CONVERSATION_ENSURE_SQL,
    _CONVERSATION_LOCK_SQL,
    _EVENT_LOOKUP_SQL,
    _OUTBOUND_EVENT_INSERT_SQL,
    _canonical_payload_json,
)
from app.durable.tenant_registry import get_registry_engine
from app.durable.zernio_conversations import find_zernio_route
from app.identity_primitives import normalize_phone, stable_id
from app.settings import Settings

logger = logging.getLogger(__name__)

# A ``pending`` row older than this is a crashed/abandoned attempt: the same
# send_id may be retried (the provider Idempotency-Key makes that safe).
PENDING_STALE_SECONDS = 120
# The operator is waiting on an HTTP call: fewer provider retries than the outbox.
MAX_PROVIDER_ATTEMPTS = 2

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f]")


class WebappSendRequest(BaseModel):
    """Strict body of ``POST /v1/webapp/send``."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=128)
    phone: str = Field(pattern=r"^[0-9]{10,15}$")
    text: str = Field(min_length=1, max_length=4096)
    actor_id: str = Field(min_length=1, max_length=256)
    send_id: str = Field(min_length=8, max_length=128)

    @field_validator("text")
    @classmethod
    def _text_is_printable(cls, value: str) -> str:
        # Line breaks and tabs are legitimate in a chat message; every other
        # control character is rejected.
        if _CONTROL_CHARS.search(value) or not value.strip():
            raise ValueError("text must be non-blank and free of control characters")
        return value

    @field_validator("tenant_id", "actor_id", "send_id")
    @classmethod
    def _no_control_chars(cls, value: str) -> str:
        if _CONTROL_CHARS.search(value) or "\n" in value or "\r" in value or "\t" in value:
            raise ValueError("control characters are not allowed")
        return value

    @property
    def destination_jid(self) -> str:
        return f"{self.phone}@s.whatsapp.net"

    def request_hash(self) -> str:
        return hashlib.sha256(
            f"{self.phone}\0{self.text}".encode()
        ).hexdigest()

    def provider_idempotency_key(self) -> str:
        return "webapp-send:" + hashlib.sha256(
            f"{self.tenant_id}\0{self.send_id}".encode()
        ).hexdigest()


class SendResult(BaseModel):
    status_code: int
    body: dict[str, Any]


def _error(status_code: int, error: str) -> SendResult:
    return SendResult(status_code=status_code, body={"error": error})


_ZERNIO_INSTANCES_SQL = text(
    """
    /* webapp_send.zernio_route */
    SELECT instance
    FROM genius_next.tenant_registry
    WHERE tenant_id = :tenant_id
      AND provider = 'zernio'
      AND enabled = true
    """
)

_CLAIM_INSERT_SQL = text(
    """
    /* webapp_send.claim */
    INSERT INTO genius_next.webapp_sends
        (tenant_id, send_id, request_hash, phone, actor_id, status)
    VALUES (:tenant_id, :send_id, :request_hash, :phone, :actor_id, 'pending')
    ON CONFLICT (tenant_id, send_id) DO NOTHING
    RETURNING send_id
    """
)

_CLAIM_RECLAIM_SQL = text(
    """
    /* webapp_send.reclaim */
    UPDATE genius_next.webapp_sends
    SET status = 'pending', actor_id = :actor_id, updated_at = now()
    WHERE tenant_id = :tenant_id
      AND send_id = :send_id
      AND request_hash = :request_hash
      AND (
            status = 'uncertain'
         OR (status = 'pending'
             AND updated_at < now() - make_interval(secs => :stale_seconds))
      )
    RETURNING send_id
    """
)

_CLAIM_READ_SQL = text(
    """
    /* webapp_send.read */
    SELECT request_hash, status, error, message_id
    FROM genius_next.webapp_sends
    WHERE tenant_id = :tenant_id AND send_id = :send_id
    """
)

_FINISH_SQL = text(
    """
    /* webapp_send.finish */
    UPDATE genius_next.webapp_sends
    SET status = :status, error = :error, message_id = :message_id, updated_at = now()
    WHERE tenant_id = :tenant_id AND send_id = :send_id
    """
)


def record_manual_outbound_event_with_session(
    session: Session,
    *,
    tenant_id: str,
    provider: str,
    instance: str,
    remote_jid: str,
    text_content: str,
    message_id: str,
    idempotency_key: str,
) -> bool:
    """Ensure one operator-sent text exists as an outbound event (idempotent).

    Same row shape as ``record_outbound_event_with_session`` (an Evolution-style
    ``send.message`` envelope under the provider message id, ingest workflow id
    ``outbound:...``) but without ``ClaimedOutbox``, whose 1024-char text cap is
    below the 4096 a person may type.
    """
    identity = {
        "tenant_id": tenant_id,
        "provider": provider,
        "instance": instance,
        "remote_jid": remote_jid,
        "message_id": message_id,
    }

    def same(existing: Any) -> bool:
        return (
            existing is not None
            and existing.get("direction") == "outbound"
            and existing.get("instance") == instance
            and existing.get("text_content") == text_content
        )

    existing = session.execute(_EVENT_LOOKUP_SQL, identity).mappings().first()
    if existing is not None:
        return same(existing)

    session.execute(_CONVERSATION_ENSURE_SQL, identity)
    locked = session.execute(_CONVERSATION_LOCK_SQL, identity).mappings().first()
    if locked is None:
        raise RuntimeError("conversation lock row was not created")
    existing = session.execute(_EVENT_LOOKUP_SQL, identity).mappings().first()
    if existing is not None:
        return same(existing)

    sequence = int(locked["next_sequence"])
    session.execute(_CONVERSATION_ADVANCE_SQL, {**identity, "next_sequence": sequence + 1})
    payload = EvolutionWebhook.model_validate(
        {
            "event": "send.message",
            "instance": instance,
            "data": {
                "key": {"id": message_id, "remoteJid": remote_jid, "fromMe": True},
                "message": {"conversation": text_content},
                "messageType": "conversation",
            },
        }
    )
    sanitized = payload.model_dump(by_alias=True, exclude_none=True, mode="json")
    session.execute(
        _OUTBOUND_EVENT_INSERT_SQL,
        {
            **identity,
            "phone": normalize_phone(remote_jid),
            "message_type": payload.data.message_type,
            "direction": "outbound",
            "text_content": text_content,
            "push_name": None,
            "sanitized_payload": sanitized,
            "event_type": "send.message",
            "event_hash": hashlib.sha256(_canonical_payload_json(sanitized)).hexdigest(),
            "conversation_sequence": sequence,
            "ingest_workflow_id": stable_id("outbound", idempotency_key),
        },
    )
    inserted = session.execute(_EVENT_LOOKUP_SQL, identity).mappings().first()
    if not same(inserted):
        raise RuntimeError("outbound event insert did not preserve the sent message")
    return True


class _Store:
    """The database touches of one send, on the shared registry engine."""

    def __init__(self, database_url: str) -> None:
        self._engine = get_registry_engine(database_url)

    def zernio_instances(self, tenant_id: str) -> list[str]:
        with Session(self._engine) as session:
            return list(
                session.execute(_ZERNIO_INSTANCES_SQL, {"tenant_id": tenant_id}).scalars()
            )

    def claim(self, req: WebappSendRequest) -> tuple[Literal["claimed", "existing"], dict | None]:
        params = {
            "tenant_id": req.tenant_id,
            "send_id": req.send_id,
            "request_hash": req.request_hash(),
            "phone": req.phone,
            "actor_id": req.actor_id,
        }
        with Session(self._engine) as session, session.begin():
            if session.execute(_CLAIM_INSERT_SQL, params).first() is not None:
                return "claimed", None
            if session.execute(
                _CLAIM_RECLAIM_SQL, {**params, "stale_seconds": PENDING_STALE_SECONDS}
            ).first() is not None:
                return "claimed", None
            row = session.execute(
                _CLAIM_READ_SQL,
                {"tenant_id": req.tenant_id, "send_id": req.send_id},
            ).mappings().first()
            return "existing", dict(row) if row is not None else None

    def finish(
        self,
        req: WebappSendRequest,
        *,
        status: str,
        error: str | None = None,
        message_id: str | None = None,
    ) -> None:
        with Session(self._engine) as session, session.begin():
            session.execute(
                _FINISH_SQL,
                {
                    "tenant_id": req.tenant_id,
                    "send_id": req.send_id,
                    "status": status,
                    "error": error,
                    "message_id": message_id,
                },
            )

    def record_event(
        self, req: WebappSendRequest, *, route: dict[str, Any], message_id: str
    ) -> None:
        with Session(self._engine) as session, session.begin():
            record_manual_outbound_event_with_session(
                session,
                tenant_id=req.tenant_id,
                provider="zernio",
                instance=route["instance"],
                remote_jid=route["remote_jid"],
                text_content=req.text,
                message_id=message_id,
                idempotency_key=req.provider_idempotency_key(),
            )


def perform_send(
    req: WebappSendRequest,
    *,
    settings: Settings,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] | None = None,
    store: _Store | None = None,
    route_finder: Callable[..., dict[str, Any] | None] | None = None,
) -> SendResult:
    """Run one operator send and map every outcome to ``(status, body)``.

    Raises only on infrastructure failure *before* anything was sent (the router
    turns that into 503); once the provider accepted the text, the answer is 200.
    """
    store = store or _Store(settings.DATABASE_URL)

    if not store.zernio_instances(req.tenant_id):
        return _error(404, "no_zernio_route")
    if not destination_allowed(
        req.destination_jid, settings.delivery_allowlist_jids, "zernio"
    ):
        return _error(403, "destination_not_allowlisted")
    if settings.DELIVERY_MODE != "live":
        return _error(409, "delivery_not_live")

    route = (route_finder or find_zernio_route)(
        tenant_id=req.tenant_id,
        remote_jid=req.destination_jid,
        database_url=settings.DATABASE_URL,
    )
    if route is None:
        return _error(409, "no_conversation")

    state, existing = store.claim(req)
    if state == "existing":
        return _replay(req, existing, route, store)

    kwargs: dict[str, Any] = {}
    if sleep is not None:
        kwargs["sleep"] = sleep
    outcome = zernio_delivery.deliver_text(
        conversation_id=route["conversation_id"],
        account_id=route["instance"],
        text=req.text,
        idempotency_key=req.provider_idempotency_key(),
        settings=settings,
        client=client,
        max_attempts=MAX_PROVIDER_ATTEMPTS,
        **kwargs,
    )
    result = outcome.provider_result

    if outcome.status == "sent" and outcome.provider_message_id:
        message_id = outcome.provider_message_id
        try:
            store.finish(req, status="sent", message_id=message_id)
        except Exception:
            logger.error(
                "webapp send delivered but not marked: tenant=%s send_id=%s actor=%s",
                req.tenant_id, req.send_id, req.actor_id, exc_info=True,
            )
        _record_event_best_effort(store, req, route, message_id)
        logger.info(
            "webapp send ok: tenant=%s send_id=%s actor=%s message_id=%s",
            req.tenant_id, req.send_id, req.actor_id, message_id,
        )
        return SendResult(status_code=200, body={"status": "sent", "message_id": message_id})

    reason = str(result.get("reason") or "rejected")
    if outcome.status == "failed" and "status_code" in result:
        # Terminal 4xx from Zernio (window closed, template, auth, ...): sticky.
        store.finish(req, status="failed", error=reason)
        logger.warning(
            "webapp send refused: tenant=%s send_id=%s actor=%s reason=%s",
            req.tenant_id, req.send_id, req.actor_id, reason,
        )
        return _error(409, reason)

    # Ambiguous or not attempted (5xx, transport, missing key): retryable with the
    # same send_id, which reuses the provider Idempotency-Key.
    store.finish(req, status="uncertain", error=reason)
    logger.warning(
        "webapp send unavailable: tenant=%s send_id=%s actor=%s reason=%s",
        req.tenant_id, req.send_id, req.actor_id, reason,
    )
    return _error(503 if reason == "configuration" else 502, "provider_unavailable")


def _record_event_best_effort(
    store: _Store, req: WebappSendRequest, route: dict[str, Any], message_id: str
) -> None:
    try:
        store.record_event(req, route=route, message_id=message_id)
    except Exception:
        # The text is already with the contact; a replay of this send_id retries
        # the (idempotent) event write.
        logger.error(
            "webapp send delivered but outbound event not recorded: tenant=%s send_id=%s",
            req.tenant_id, req.send_id, exc_info=True,
        )


def _replay(
    req: WebappSendRequest,
    existing: dict[str, Any] | None,
    route: dict[str, Any],
    store: _Store,
) -> SendResult:
    """Answer a repeated (tenant_id, send_id) without sending again."""
    if existing is None or existing["request_hash"] != req.request_hash():
        return _error(409, "send_id_reused")
    status = existing["status"]
    if status == "sent" and existing["message_id"]:
        _record_event_best_effort(store, req, route, existing["message_id"])
        return SendResult(
            status_code=200,
            body={"status": "sent", "message_id": existing["message_id"]},
        )
    if status == "failed":
        return _error(409, existing["error"] or "rejected")
    return _error(409, "send_in_progress")
