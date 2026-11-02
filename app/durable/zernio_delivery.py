"""Guarded Zernio inbox send: answer a WhatsApp contact inside the 24h window.

Mirrors the Evolution ``send_text`` contract: one reduced, sanitized
``OutboxFinalization``, never a raw provider body.

  * 2xx with a message id            -> ``sent``
  * 429 / 5xx / transport / 409-busy -> bounded retry with the SAME
    ``Idempotency-Key`` (``Retry-After`` honoured), then ``uncertain``
  * any other 4xx (window closed, template required, auth, unknown
    conversation...)                 -> terminal ``failed`` with a readable
    reason, never retried.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx

from app.durable.persistence import ClaimedOutbox, OutboxFinalization
from app.durable.zernio_conversations import lookup_zernio_conversation
from app.contracts import TemplateCommand
from app.settings import Settings

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
MAX_RETRY_WAIT_SECONDS = 30.0
_CODE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_WINDOW_CODES = frozenset({"131047", "TEMPLATE_REQUIRED"})

_DETAIL = {
    "configuration": "ZERNIO_API_KEY nao configurada",
    "no_conversation": "sem conversationId do Zernio para este contato (ele nunca escreveu para este numero)",
    "conversation_lookup_failed": "falha ao ler o conversationId do contato",
    "window_closed": "janela de 24h fechada ou template exigido pela Cloud API",
    "recipient_rate_limited": "limite de mensagens por destinatario da Cloud API",
    "auth_rejected": "Zernio recusou a credencial ou a permissao",
    "not_found": "Zernio nao encontrou a conversa ou a conta",
    "rejected": "Zernio recusou o envio (4xx)",
    "media_unsupported": "envio de midia nao suportado pelo canal Zernio",
    "missing_template": "comando send_template sem referencia de modelo",
}


def _finalization_failed(reason: str, **extra: str | int | bool | None) -> OutboxFinalization:
    return OutboxFinalization(
        status="failed",
        provider_result={"reason": reason, "detail": _DETAIL.get(reason), **extra},
    )


def _retry_after_seconds(header: str | None) -> float | None:
    if not header:
        return None
    header = header.strip()
    try:
        return max(float(header), 0.0)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - datetime.now(UTC)).total_seconds(), 0.0)


def _error_code(body: Any) -> str | None:
    """Sanitized provider error code (Zernio ``code`` or Meta's, never free text)."""
    if not isinstance(body, dict):
        return None
    candidates: list[Any] = [body.get("code")]
    platform_error = body.get("platformError")
    if isinstance(platform_error, dict):
        candidates.append(platform_error.get("code"))
        error_data = platform_error.get("error")
        if isinstance(error_data, dict):
            candidates.append(error_data.get("code"))
    for candidate in candidates:
        if isinstance(candidate, (str, int)) and not isinstance(candidate, bool):
            value = str(candidate)
            if _CODE.fullmatch(value):
                return value
    return None


def _json_or_none(response: httpx.Response) -> Any:
    if not response.content or len(response.content) > 262_144:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _classify_terminal(status_code: int, code: str | None, body: Any) -> str:
    if code in _WINDOW_CODES:
        return "window_closed"
    if body is not None and "131047" in json.dumps(body, default=str)[:4_000]:
        return "window_closed"
    if code == "131056":
        return "recipient_rate_limited"
    if status_code in (401, 403):
        return "auth_rejected"
    if status_code == 404:
        return "not_found"
    return "rejected"


def _is_retryable(status_code: int, code: str | None) -> bool:
    return (
        status_code == 429
        or status_code >= 500
        or (status_code == 409 and code == "idempotency_conflict")
    )


def send_text(
    claimed: ClaimedOutbox,
    *,
    settings: Settings,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    conversation_lookup: Callable[..., str | None] = lookup_zernio_conversation,
) -> OutboxFinalization:
    """Send one text into the contact's Zernio conversation and reduce the outcome."""
    if settings.ZERNIO_API_KEY is None:
        return _finalization_failed("configuration")
    try:
        conversation_id = conversation_lookup(
            tenant_id=claimed.tenant_id,
            instance=claimed.instance,
            remote_jid=claimed.destination_jid,
        )
    except Exception:
        logger.warning("zernio conversation lookup failed", exc_info=True)
        return _finalization_failed("conversation_lookup_failed")
    if not conversation_id:
        return _finalization_failed("no_conversation")
    return deliver_text(
        conversation_id=conversation_id,
        account_id=claimed.instance,
        text=claimed.text,
        idempotency_key=claimed.idempotency_key,
        settings=settings,
        client=client,
        sleep=sleep,
    )


def _messages_url(settings: Settings, conversation_id: str) -> str:
    return (
        f"{settings.ZERNIO_API_URL}/v1/inbox/conversations/"
        f"{quote(conversation_id, safe='')}/messages"
    )


def _deliver_message(
    *,
    url: str,
    payload: dict[str, Any],
    idempotency_key: str,
    settings: Settings,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int = MAX_ATTEMPTS,
) -> OutboxFinalization:
    """POST one message payload and reduce the outcome (shared retry policy).

    The same ``Idempotency-Key`` is reused across retries (and across caller
    retries of the same logical send), so Zernio de-duplicates ambiguous
    attempts.
    """
    if settings.ZERNIO_API_KEY is None:
        return _finalization_failed("configuration")
    headers = {
        "Authorization": f"Bearer {settings.ZERNIO_API_KEY.get_secret_value()}",
        "Idempotency-Key": idempotency_key,
    }

    owned_client = client is None
    http = client or httpx.Client(timeout=settings.DELIVERY_TIMEOUT_SECONDS)
    try:
        for attempt in range(1, max_attempts + 1):
            retry_after: float | None = None
            try:
                response = http.post(url, headers=headers, json=payload)
            except httpx.TransportError:
                last: OutboxFinalization = OutboxFinalization(
                    status="uncertain",
                    provider_result={"reason": "transport_ambiguous", "attempts": attempt},
                )
            except Exception:
                logger.warning("zernio send failed", exc_info=True)
                return _finalization_failed("request_failure", attempts=attempt)
            else:
                body = _json_or_none(response)
                status_code = response.status_code
                if 200 <= status_code < 300:
                    return _sent_or_uncertain(body, status_code, attempt)
                code = _error_code(body)
                if not _is_retryable(status_code, code):
                    reason = _classify_terminal(status_code, code, body)
                    return _finalization_failed(
                        reason,
                        status_code=status_code,
                        provider_code=code,
                        attempts=attempt,
                    )
                retry_after = _retry_after_seconds(response.headers.get("retry-after"))
                last = OutboxFinalization(
                    status="uncertain",
                    provider_result={
                        "reason": "provider_status",
                        "status_code": status_code,
                        "attempts": attempt,
                    },
                )
            if attempt < max_attempts:
                sleep(
                    min(retry_after, MAX_RETRY_WAIT_SECONDS)
                    if retry_after is not None
                    else float(2 ** (attempt - 1))
                )
        return last
    finally:
        if owned_client:
            http.close()


def deliver_text(
    *,
    conversation_id: str,
    account_id: str,
    text: str,
    idempotency_key: str,
    settings: Settings,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int = MAX_ATTEMPTS,
) -> OutboxFinalization:
    """POST one text into a known Zernio conversation and reduce the outcome.

    Shared by the outbox (:func:`send_text`) and the operator send endpoint.
    """
    if settings.ZERNIO_API_KEY is None:
        return _finalization_failed("configuration")
    return _deliver_message(
        url=_messages_url(settings, conversation_id),
        payload={"accountId": account_id, "message": text},
        idempotency_key=idempotency_key,
        settings=settings,
        client=client,
        sleep=sleep,
        max_attempts=max_attempts,
    )


def template_send_payload(account_id: str, template: TemplateCommand) -> dict[str, Any]:
    """Meta Cloud API send shape for one approved template (Zernio wrapper).

    ``components`` carries the positional body variables; it is omitted when the
    template has none, so Zernio forwards the exact Meta payload.
    """
    element: dict[str, Any] = {
        "name": template.name,
        "language": template.language,
    }
    if template.variables:
        element["components"] = [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": value} for value in template.variables
                ],
            }
        ]
    return {"accountId": account_id, "template": {"elements": [element]}}


def deliver_template(
    *,
    conversation_id: str,
    account_id: str,
    template: TemplateCommand,
    idempotency_key: str,
    settings: Settings,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int = MAX_ATTEMPTS,
) -> OutboxFinalization:
    """POST one approved template into a known Zernio conversation."""
    if settings.ZERNIO_API_KEY is None:
        return _finalization_failed("configuration")
    return _deliver_message(
        url=_messages_url(settings, conversation_id),
        payload=template_send_payload(account_id, template),
        idempotency_key=idempotency_key,
        settings=settings,
        client=client,
        sleep=sleep,
        max_attempts=max_attempts,
    )


def send_template(
    claimed: ClaimedOutbox,
    *,
    settings: Settings,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    conversation_lookup: Callable[..., str | None] = lookup_zernio_conversation,
) -> OutboxFinalization:
    """Send one approved template into the contact's Zernio conversation."""
    if settings.ZERNIO_API_KEY is None:
        return _finalization_failed("configuration")
    if claimed.template is None:
        return _finalization_failed("missing_template")
    try:
        conversation_id = conversation_lookup(
            tenant_id=claimed.tenant_id,
            instance=claimed.instance,
            remote_jid=claimed.destination_jid,
        )
    except Exception:
        logger.warning("zernio conversation lookup failed", exc_info=True)
        return _finalization_failed("conversation_lookup_failed")
    if not conversation_id:
        return _finalization_failed("no_conversation")
    return deliver_template(
        conversation_id=conversation_id,
        account_id=claimed.instance,
        template=claimed.template,
        idempotency_key=claimed.idempotency_key,
        settings=settings,
        client=client,
        sleep=sleep,
    )



def _sent_or_uncertain(body: Any, status_code: int, attempt: int) -> OutboxFinalization:
    data = body.get("data") if isinstance(body, dict) else None
    message_id = (data or {}).get("messageId") if isinstance(data, dict) else None
    if not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 512:
        return OutboxFinalization(
            status="uncertain",
            provider_result={
                "reason": "provider_protocol",
                "status_code": status_code,
                "attempts": attempt,
            },
        )
    return OutboxFinalization(
        status="sent",
        provider_message_id=message_id.strip(),
        provider_result={"status_code": status_code, "attempts": attempt},
    )


def send_media(claimed: ClaimedOutbox, *, settings: Settings) -> OutboxFinalization:
    """Media replies are not supported on the Zernio channel yet."""
    del claimed, settings
    return _finalization_failed("media_unsupported")
