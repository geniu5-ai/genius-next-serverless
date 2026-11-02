"""PII-free durable identifiers and trusted Evolution event construction."""

from __future__ import annotations

import hashlib
import json

from app.contracts import EventIdentity, EvolutionWebhook, TrustedIngress
from app.identity_primitives import normalize_phone, stable_id


def _canonical_json(payload: dict) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def build_trusted_ingress(
    webhook: EvolutionWebhook,
    *,
    tenant_id: str,
    provider: str = "evolution",
) -> TrustedIngress:
    """Convert a validated untrusted webhook into the strict durable envelope."""
    sanitized_payload = EvolutionWebhook.model_validate(
        webhook.model_dump(by_alias=True, exclude_none=True, mode="json")
    )
    canonical_payload = sanitized_payload.model_dump(
        by_alias=True, exclude_none=True, mode="json"
    )
    event_hash = hashlib.sha256(_canonical_json(canonical_payload)).hexdigest()
    key = webhook.data.key
    identity = EventIdentity(
        tenant_id=tenant_id,
        provider=provider,
        instance=webhook.instance,
        event_type=webhook.event,
        message_id=key.id,
        remote_jid=key.remote_jid,
    )
    return TrustedIngress(
        tenant_id=tenant_id,
        identity=identity,
        conversation_id=stable_id(
            "conversation",
            tenant_id,
            provider,
            webhook.instance,
            key.remote_jid,
        ),
        ingest_workflow_id=stable_id(
            "ingest",
            tenant_id,
            provider,
            webhook.event,
            key.id,
            event_hash,
        ),
        phone=normalize_phone(key.remote_jid),
        direction="outbound" if key.from_me else "inbound",
        text_content=webhook.text_content,
        event_hash=event_hash,
        sanitized_payload=sanitized_payload,
    )
