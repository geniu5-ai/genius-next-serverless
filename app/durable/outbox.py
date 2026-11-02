"""Guarded, auditable outbox dispatch (Evolution and Zernio)."""

from __future__ import annotations

import random
from typing import Literal

import logging
import time

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from app import observability
from app.contracts import OutboxCommand
from app.durable.persistence import (
    ClaimedOutbox,
    OutboxFinalization,
    automation_allowed_for_delivery_txn,
    claim_outbox_txn,
    finalize_outbox_txn,
    outbox_route_registered_txn,
    reclaim_outbox_txn,
)
from app.durable import zernio_delivery
from app.durable import template_send
from app.identity_primitives import (
    brazilian_mobile_jid_variants,
    direct_jids_equivalent,
    normalize_phone,
    stable_id,
)
from app.ops_alert import ops_alert_step, ops_alert_text, send_ops_alert
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)

_EXTERNAL = ConfigDict(extra="ignore", populate_by_name=True, str_strip_whitespace=True)


class _SendKey(BaseModel):
    model_config = _EXTERNAL

    id: str = Field(min_length=1, max_length=512)
    remote_jid: str = Field(alias="remoteJid", min_length=1, max_length=512)
    from_me: bool = Field(alias="fromMe")


class _SendResponse(BaseModel):
    model_config = _EXTERNAL

    key: _SendKey


def _secret_value(secret: SecretStr | None) -> str:
    if secret is None:
        raise ValueError("Evolution API key is not configured")
    return secret.get_secret_value()


def _provider_number(destination_jid: str) -> str:
    """Map a destination JID to Evolution's ``number`` sendText/sendMedia param.

    Phone JIDs are sent as the bare digit-only number (Evolution appends
    ``@s.whatsapp.net``).  A LinkedID (``@lid``) has no phone number, so it
    must be sent as the full ``<lid>@lid`` JID — a bare LID would be
    interpreted as a phone number and addressed to the wrong recipient.
    """
    if destination_jid.endswith("@lid"):
        return destination_jid
    return normalize_phone(destination_jid)


def send_text(
    claimed: ClaimedOutbox,
    *,
    settings: Settings,
    client: httpx.Client | None = None,
) -> OutboxFinalization:
    """Attempt one sendText call and reduce its response to a terminal outcome."""
    if settings.EVOLUTION_URL is None:
        return OutboxFinalization(
            status="failed", provider_result={"reason": "configuration"}
        )

    owned_client = client is None
    http = client or httpx.Client(timeout=settings.DELIVERY_TIMEOUT_SECONDS)
    try:
        response = http.post(
            f"{settings.EVOLUTION_URL}/message/sendText/{claimed.instance}",
            headers={"apikey": _secret_value(settings.EVOLUTION_API_KEY)},
            json={
                "number": _provider_number(claimed.destination_jid),
                "text": claimed.text,
                "linkPreview": True,
            },
        )
    except httpx.TransportError:
        observability.annotate(error_type="TransportError")
        return OutboxFinalization(
            status="uncertain",
            provider_result={"reason": "transport_ambiguous"},
        )
    except Exception as exc:
        observability.annotate(error_type=type(exc).__name__)
        logger.warning("outbox send failed", exc_info=True)
        return OutboxFinalization(
            status="failed", provider_result={"reason": "request_failure"}
        )
    finally:
        if owned_client:
            http.close()

    if response.status_code != 201:
        return OutboxFinalization(
            status="uncertain" if response.status_code >= 500 else "failed",
            provider_result={
                "reason": "provider_status",
                "status_code": response.status_code,
            },
        )
    if len(response.content) > settings.DELIVERY_RESPONSE_MAX_BYTES:
        return OutboxFinalization(
            status="uncertain", provider_result={"reason": "provider_protocol"}
        )
    try:
        parsed = _SendResponse.model_validate(response.json())
    except (ValidationError, ValueError):
        return OutboxFinalization(
            status="uncertain", provider_result={"reason": "provider_protocol"}
        )
    if not parsed.key.from_me or not direct_jids_equivalent(
        claimed.destination_jid, parsed.key.remote_jid
    ):
        return OutboxFinalization(
            status="uncertain",
            provider_message_id=parsed.key.id,
            provider_result={"reason": "provider_protocol"},
        )
    return OutboxFinalization(
        status="sent",
        provider_message_id=parsed.key.id,
        provider_result={"status_code": 201, "from_me": True},
    )


def send_media(
    claimed: ClaimedOutbox,
    *,
    settings: Settings,
    client: httpx.Client | None = None,
) -> OutboxFinalization:
    """Attempt one sendMedia call and reduce its response to a terminal outcome."""
    if settings.EVOLUTION_URL is None:
        return OutboxFinalization(
            status="failed", provider_result={"reason": "configuration"}
        )
    if claimed.media_base64 is None:
        return OutboxFinalization(
            status="failed", provider_result={"reason": "missing_media"}
        )

    owned_client = client is None
    http = client or httpx.Client(timeout=settings.DELIVERY_TIMEOUT_SECONDS)
    try:
        response = http.post(
            f"{settings.EVOLUTION_URL}/message/sendMedia/{claimed.instance}",
            headers={"apikey": _secret_value(settings.EVOLUTION_API_KEY)},
            json={
                "number": _provider_number(claimed.destination_jid),
                "mediatype": "audio",
                "media": claimed.media_base64,
                "mimetype": claimed.mime_type or "audio/mpeg",
                "caption": claimed.text,
            },
        )
    except httpx.TransportError:
        observability.annotate(error_type="TransportError")
        return OutboxFinalization(
            status="uncertain",
            provider_result={"reason": "transport_ambiguous"},
        )
    except Exception as exc:
        observability.annotate(error_type=type(exc).__name__)
        logger.warning("outbox media send failed", exc_info=True)
        return OutboxFinalization(
            status="failed", provider_result={"reason": "request_failure"}
        )
    finally:
        if owned_client:
            http.close()

    if response.status_code != 201:
        return OutboxFinalization(
            status="uncertain" if response.status_code >= 500 else "failed",
            provider_result={
                "reason": "provider_status",
                "status_code": response.status_code,
            },
        )
    if len(response.content) > settings.DELIVERY_RESPONSE_MAX_BYTES:
        return OutboxFinalization(
            status="uncertain", provider_result={"reason": "provider_protocol"}
        )
    try:
        parsed = _SendResponse.model_validate(response.json())
    except (ValidationError, ValueError):
        return OutboxFinalization(
            status="uncertain", provider_result={"reason": "provider_protocol"}
        )
    if not parsed.key.from_me or not direct_jids_equivalent(
        claimed.destination_jid, parsed.key.remote_jid
    ):
        return OutboxFinalization(
            status="uncertain",
            provider_message_id=parsed.key.id,
            provider_result={"reason": "provider_protocol"},
        )
    return OutboxFinalization(
        status="sent",
        provider_message_id=parsed.key.id,
        provider_result={"status_code": 201, "from_me": True},
    )


def destination_allowed(
    destination_jid: str, allowlist: tuple[str, ...] | frozenset[str], provider: str
) -> bool:
    """Whether the delivery allowlist admits ``destination_jid``.

    Zernio only: the Cloud API stores a Brazilian mobile without the ninth digit
    while the registered number carries it, so either form admits the other.
    Evolution keeps the exact-match rule.
    """
    if "*" in allowlist or destination_jid in allowlist:
        return True
    if provider != "zernio":
        return False
    return any(v in allowlist for v in brazilian_mobile_jid_variants(destination_jid))


def load_delivery_allowlist_step() -> tuple[str, ...]:
    """Checkpoint deployment policy so replay cannot observe a changed allowlist."""
    return tuple(sorted(get_settings().delivery_allowlist_jids))


def send_text_step(claimed: ClaimedOutbox) -> OutboxFinalization:
    """The only runtime path allowed to invoke Evolution ``sendText``."""
    return send_text(claimed, settings=get_settings())


def send_media_step(claimed: ClaimedOutbox) -> OutboxFinalization:
    """The only runtime path allowed to invoke Evolution ``sendMedia``."""
    return send_media(claimed, settings=get_settings())


def send_via_zernio(
    claimed: ClaimedOutbox,
    *,
    settings: Settings,
) -> OutboxFinalization:
    """Deliver through Zernio; manager notices may fall back to Evolution.

    The fallback runs only after Zernio terminally REFUSED the notice (nothing
    was sent), never after an ambiguous outcome, so it cannot duplicate it.
    """
    if claimed.action == "send_media":
        return zernio_delivery.send_media(claimed, settings=settings)
    outcome = zernio_delivery.send_text(claimed, settings=settings)
    if claimed.kind != "team" or outcome.status in {"sent", "recorded"}:
        return outcome

    zernio_reason = outcome.provider_result.get("reason")
    fallback_instance = settings.HANDOFF_FALLBACK_EVOLUTION_INSTANCE
    if outcome.status != "failed" or not fallback_instance:
        logger.error(
            "handoff notice to manager NOT delivered via zernio "
            "(tenant=%s turn=%s ordinal=%s status=%s reason=%s fallback=%s)",
            claimed.tenant_id,
            claimed.turn_id,
            claimed.ordinal,
            outcome.status,
            zernio_reason,
            "none" if not fallback_instance else "skipped_ambiguous",
        )
        # Dentro de ``send_zernio_step``, aqui vai o send direto — o freio de
        # 30 min por chave ja impede reenvio quando o job repete.
        send_ops_alert(
            f"aviso_gestor:{claimed.tenant_id}:{claimed.turn_id}:{claimed.ordinal}",
            ops_alert_text(
                "aviso ao owner nao entregue",
                tenant_id=claimed.tenant_id,
                contact=claimed.destination_jid,
                error=(
                    f"zernio status={outcome.status} reason={zernio_reason} "
                    f"fallback={'sem instancia' if not fallback_instance else 'nao tentado'}"
                ),
            ),
        )
        return outcome

    fallback = send_text(
        claimed.model_copy(update={"instance": fallback_instance}),
        settings=settings,
    )
    result = {
        **fallback.provider_result,
        "via": "evolution_fallback",
        "zernio_reason": zernio_reason,
    }
    if fallback.status != "sent":
        logger.error(
            "handoff notice to manager failed on zernio AND on the evolution "
            "fallback (tenant=%s turn=%s ordinal=%s zernio_reason=%s "
            "fallback_status=%s)",
            claimed.tenant_id,
            claimed.turn_id,
            claimed.ordinal,
            zernio_reason,
            fallback.status,
        )
        send_ops_alert(
            f"aviso_gestor:{claimed.tenant_id}:{claimed.turn_id}:{claimed.ordinal}",
            ops_alert_text(
                "aviso ao owner nao entregue",
                tenant_id=claimed.tenant_id,
                contact=claimed.destination_jid,
                error=(
                    f"zernio reason={zernio_reason} "
                    f"fallback status={fallback.status}"
                ),
            ),
        )
    return OutboxFinalization(
        status=fallback.status,
        provider_message_id=fallback.provider_message_id,
        provider_result=result,
    )


def send_zernio_step(claimed: ClaimedOutbox) -> OutboxFinalization:
    """The only runtime path allowed to invoke the Zernio send API."""
    return send_via_zernio(claimed, settings=get_settings())


def send_template_step(claimed: ClaimedOutbox) -> OutboxFinalization:
    """The only runtime path allowed to invoke the Zernio template send."""
    return zernio_delivery.send_template(claimed, settings=get_settings())


def _follow_up_delivery_allowed(claimed: ClaimedOutbox) -> bool:
    """Ask the canonical follow-up transaction immediately before provider I/O."""
    if claimed.follow_up is None:
        return False
    # Imported lazily because followup itself imports this module to dispatch
    # its synthetic commands.  The transaction owns the same tenant, phone,
    # origin-sequence, status, handoff and newer-inbound checks as scheduling.
    from app.durable.followup import should_continue_follow_up_txn

    return should_continue_follow_up_txn(
        claimed.tenant_id,
        claimed.follow_up.phone,
        claimed.follow_up.origin_turn_id,
    )


def _normal_reply_delivery_allowed(tenant_id: str, destination_jid: str) -> bool:
    """Recheck ownership from the canonical destination immediately before send."""
    try:
        normalized_destination = normalize_phone(destination_jid)
    except ValueError:
        return False
    return automation_allowed_for_delivery_txn(tenant_id, normalized_destination)


def _alert_customer_delivery_failed(
    claimed: ClaimedOutbox, outcome: OutboxFinalization
) -> None:
    """Paciente ficou sem mensagem (janela de 24h, sem conversa, 4xx/5xx...).

    ``destination_not_allowlisted`` fica fora de proposito: e recusa de
    configuracao, nao quebra de flow. O alerta nunca pode mudar a entrega --
    por isso engole a propria falha.
    """
    if (
        claimed.kind != "customer"
        or outcome.status != "failed"
        or outcome.provider_result.get("reason") == "destination_not_allowlisted"
    ):
        return
    try:
        ops_alert_step(
            f"entrega:{claimed.idempotency_key}",
            ops_alert_text(
                "entrega ao paciente nao concluida",
                tenant_id=claimed.tenant_id,
                contact=claimed.destination_jid,
                error=f"{outcome.provider_result.get('reason')} status={outcome.status}",
            ),
        )
    except Exception:
        logger.warning("outbox: alerta operacional nao enviado", exc_info=True)


def dispatch_outbox_workflow(
    command: OutboxCommand,
) -> OutboxFinalization | None:
    """Um span por mensagem no envio: event_id, tenant, provedor e resultado (inclui falhas)."""
    with observability.message_span(
        "outbox.send",
        event_id=command.idempotency_key,
        tenant_id=command.tenant_id,
        instance=command.instance,
        action=command.action,
        purpose=command.purpose,
        kind=command.kind,
        delivery_mode=command.delivery_mode,
        contact=command.destination_jid,
    ) as span:
        outcome = _dispatch_outbox_unspanned(command)
        observability.record_delivery(span, outcome)
        return outcome


def _dispatch_outbox_unspanned(
    command: OutboxCommand,
) -> OutboxFinalization | None:
    """Lease, authorize, attempt at most once, and finalize one command."""
    lease_owner = stable_id("outbox-lease", command.idempotency_key)
    # Reclaim stuck rows: claimed with expired lease, never sent.
    reclaim_outbox_txn(command)
    claimed = claim_outbox_txn(command, lease_owner)
    if claimed is None:
        return None
    observability.annotate(provider=claimed.provider)

    if claimed.purpose == "follow_up" and not _follow_up_delivery_allowed(claimed):
        outcome = OutboxFinalization(
            status="failed",
            provider_result={"reason": "follow_up_ineligible_at_dispatch"},
        )
    elif claimed.delivery_mode == "record_only":
        outcome = OutboxFinalization(
            status="recorded", provider_result={"mode": "record_only"}
        )
    else:
        allowlist = load_delivery_allowlist_step()
        allowed_destination = destination_allowed(
            claimed.destination_jid, allowlist, claimed.provider
        )
        registered_route = allowed_destination and outbox_route_registered_txn(
            claimed.tenant_id, claimed.instance, claimed.provider
        )
        if not allowed_destination:
            outcome = OutboxFinalization(
                status="failed",
                provider_result={"reason": "destination_not_allowlisted"},
            )
        elif not registered_route:
            outcome = OutboxFinalization(
                status="failed",
                provider_result={"reason": "tenant_route_mismatch"},
            )
        elif (
            claimed.kind == "customer"
            and claimed.purpose == "reply"
            and claimed.action in {"send_text", "send_media"}
            and not _normal_reply_delivery_allowed(claimed.tenant_id, claimed.destination_jid)
        ):
            outcome = OutboxFinalization(
                status="failed",
                provider_result={"reason": "automation_ineligible_at_dispatch"},
            )
        elif claimed.action == "send_template":
            # Modelo da Meta fora da janela de 24h: a trava (aprovacao, janela,
            # estado, teto) e checada por ultimo, imediatamente antes do I/O.
            block_reason = template_send.template_block_reason_step(claimed)
            if block_reason is not None:
                outcome = OutboxFinalization(
                    status="failed", provider_result={"reason": block_reason}
                )
            elif claimed.provider != "zernio":
                outcome = OutboxFinalization(
                    status="failed",
                    provider_result={"reason": "template_requires_zernio"},
                )
            else:
                outcome = send_template_step(claimed)
        elif claimed.provider == "zernio":
            outcome = send_zernio_step(claimed)
        elif claimed.provider != "evolution":
            outcome = OutboxFinalization(
                status="failed",
                provider_result={"reason": "unsupported_provider"},
            )
        else:
            if claimed.action == "send_media":
                outcome = send_media_step(claimed)
            else:
                outcome = send_text_step(claimed)

    _alert_customer_delivery_failed(claimed, outcome)
    return finalize_outbox_txn(claimed, outcome)


def pacing_seconds_step(
    ordinal: int,
    delivery_mode: Literal["record_only", "live"],
) -> float:
    """Human-feel delay before dispatching block ``ordinal`` (0 = immediate).

    Pacing applies only to live delivery with pacing enabled; every other
    combination returns 0 so tests and record-only runs stay deterministic.
    The step checkpoints its random draw, so a workflow replay reuses the
    exact same delay.
    """
    if ordinal <= 0 or delivery_mode != "live":
        return 0.0
    settings = get_settings()
    if not settings.OUTPUT_PACING_ENABLED:
        return 0.0
    return random.uniform(
        settings.OUTPUT_PACING_MIN_SECONDS,
        settings.OUTPUT_PACING_MAX_SECONDS,
    )


def dispatch_outbox_batch_workflow(
    commands: list[OutboxCommand],
) -> list[OutboxFinalization | None]:
    """Dispatch multi-block human output with a pause between blocks.

    Block 0 goes out immediately; each following block waits the paced delay.
    Block order is preserved and each command goes through the at-most-once
    single dispatch. A crash between blocks leaves the rest ``pending`` in the
    outbox; ``despacha_outbox_pendente`` (app/durable/reaper.py) sends them.
    """
    results: list[OutboxFinalization | None] = []
    for ordinal, command in enumerate(commands):
        delay = pacing_seconds_step(ordinal, command.delivery_mode)
        if delay > 0:
            time.sleep(delay)
        results.append(dispatch_outbox_workflow(command))
    return results
