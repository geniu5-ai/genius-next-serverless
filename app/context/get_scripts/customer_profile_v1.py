"""Structured customer profile GET script.

Limitation: ``topicos_ja_mencionados`` is a deterministic recency heuristic
based on the latest distinct inbound texts, not semantic topic extraction.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text as _sql_text
from sqlalchemy.orm import Session

_INTERNAL = ConfigDict(extra="forbid", str_strip_whitespace=True)

_SCRIPT_NAME = "customer_profile"
_SCRIPT_VERSION = "v1"
_SCRIPT_TTL_SECONDS = 60
_SCRIPT_MAX_CHARS = 800
_SCRIPT_APPROVED_BY = "chairman-2026-08-22"
_SCRIPT_DESCRIPTION = (
    "Perfil estruturado do cliente com status, resumo e últimos tópicos inbound."
)
_TOPIC_FETCH_LIMIT = 12
_TOPIC_LIMIT = 5

_CLIENT_PROFILE_SQL = _sql_text(
    """
    /* get_script.customer_profile.client */
    SELECT
        name,
        push_name,
        status,
        automation_state,
        last_intent,
        metadata
    FROM genius_next.clients
    WHERE tenant_id = :tenant_id
      AND phone = :phone
    LIMIT 1
    """
)

_INBOUND_EVENTS_SQL = _sql_text(
    """
    /* get_script.customer_profile.inbound_events */
    SELECT text_content
    FROM genius_next.events
    WHERE tenant_id = :tenant_id
      AND phone = :phone
      AND direction = 'inbound'
      AND text_content IS NOT NULL
    ORDER BY conversation_sequence DESC, received_at DESC, id DESC
    LIMIT :limit
    """
)

_REGISTRY_UPSERT_SQL = _sql_text(
    """
    /* get_script.customer_profile.registry_upsert */
    INSERT INTO genius_next.get_script_registry (
        name,
        version,
        enabled,
        ttl_seconds,
        max_chars,
        description,
        approved_by
    ) VALUES (
        :name,
        :version,
        :enabled,
        :ttl_seconds,
        :max_chars,
        :description,
        :approved_by
    )
    ON CONFLICT (name) DO UPDATE
    SET version = EXCLUDED.version,
        ttl_seconds = EXCLUDED.ttl_seconds,
        max_chars = EXCLUDED.max_chars,
        description = EXCLUDED.description,
        approved_by = EXCLUDED.approved_by,
        approved_at = now(),
        updated_at = now()
    """
)

_CALL_LOG_SQL = _sql_text(
    """
    /* get_script.customer_profile.call_log */
    INSERT INTO genius_next.get_script_calls (
        script_name,
        version,
        tenant_id,
        turn_id
    ) VALUES (
        :script_name,
        :version,
        :tenant_id,
        :turn_id
    )
    """
)


class CustomerProfileV1(BaseModel):
    model_config = _INTERNAL

    nome: str | None = None
    cidade: str | None = None
    status: str
    automation_state: str
    last_intent: str | None = None
    resumo: str | None = None
    topicos_ja_mencionados: list[str] = Field(
        default_factory=list,
        max_length=_TOPIC_LIMIT,
    )


def _clean_text(value: Any, max_chars: int) -> str | None:
    if value is None:
        return None
    collapsed = " ".join(str(value).split()).strip()
    if not collapsed:
        return None
    return collapsed[:max_chars].rstrip()


def _recent_topics(rows: list[dict[str, Any]]) -> list[str]:
    selected_desc: list[str] = []
    seen: set[str] = set()
    for row in rows:
        text = _clean_text(row.get("text_content"), 120)
        if text is None:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        selected_desc.append(text)
        if len(selected_desc) == _TOPIC_LIMIT:
            break
    selected_desc.reverse()
    return selected_desc


def _payload_size(profile: CustomerProfileV1) -> int:
    return len(profile.model_dump_json())


def _fit_to_budget(profile: CustomerProfileV1) -> CustomerProfileV1:
    data = profile.model_dump()
    candidate = CustomerProfileV1.model_validate(data)
    while _payload_size(candidate) > _SCRIPT_MAX_CHARS:
        if data.get("resumo"):
            data["resumo"] = _clean_text(
                data["resumo"],
                max(len(data["resumo"]) - 32, 0),
            )
        elif data.get("last_intent"):
            data["last_intent"] = _clean_text(
                data["last_intent"],
                max(len(data["last_intent"]) - 24, 0),
            )
        elif data.get("nome"):
            data["nome"] = _clean_text(
                data["nome"],
                max(len(data["nome"]) - 16, 0),
            )
        elif data.get("cidade"):
            data["cidade"] = _clean_text(
                data["cidade"],
                max(len(data["cidade"]) - 16, 0),
            )
        elif data["topicos_ja_mencionados"]:
            data["topicos_ja_mencionados"].pop(0)
        else:
            break
        candidate = CustomerProfileV1.model_validate(data)
    return candidate


def get_customer_profile(
    session: Session,
    *,
    tenant_id: str,
    phone: str,
    turn_id: str | None = None,
) -> CustomerProfileV1 | None:
    """Return the approved customer_profile GET script payload for one client."""
    session.execute(
        _REGISTRY_UPSERT_SQL,
        {
            "name": _SCRIPT_NAME,
            "version": _SCRIPT_VERSION,
            "enabled": True,
            "ttl_seconds": _SCRIPT_TTL_SECONDS,
            "max_chars": _SCRIPT_MAX_CHARS,
            "description": _SCRIPT_DESCRIPTION,
            "approved_by": _SCRIPT_APPROVED_BY,
        },
    )
    session.execute(
        _CALL_LOG_SQL,
        {
            "script_name": _SCRIPT_NAME,
            "version": _SCRIPT_VERSION,
            "tenant_id": tenant_id,
            "turn_id": turn_id,
        },
    )

    row = session.execute(
        _CLIENT_PROFILE_SQL,
        {"tenant_id": tenant_id, "phone": phone},
    ).mappings().first()
    if row is None:
        return None

    metadata = dict(row["metadata"] or {})
    topics = session.execute(
        _INBOUND_EVENTS_SQL,
        {"tenant_id": tenant_id, "phone": phone, "limit": _TOPIC_FETCH_LIMIT},
    ).mappings().all()

    profile = CustomerProfileV1(
        nome=_clean_text(row["name"] or row["push_name"], 160),
        cidade=_clean_text(metadata.get("cidade"), 120),
        status=row["status"],
        automation_state=row["automation_state"],
        last_intent=_clean_text(row["last_intent"], 160),
        resumo=_clean_text(metadata.get("summary"), 480),
        topicos_ja_mencionados=_recent_topics(topics),
    )
    return _fit_to_budget(profile)
