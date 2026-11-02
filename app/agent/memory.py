"""Agent memory tools — PG-native conversation history and client profile.

Replaces the Evolution API history fetch with direct PG queries on
genius_next.events and genius_next.clients.  No external service,
2 ms latency, always-consistent with the durable event log.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text as _sql_text
from sqlalchemy.orm import Session

_INTERNAL = ConfigDict(extra="forbid", str_strip_whitespace=True)


class HistoryMessage(BaseModel):
    model_config = _INTERNAL

    message_id: str | None = Field(default=None, max_length=512)
    role: Literal["customer", "assistant"]
    text: str = Field(min_length=1, max_length=4096)
    received_at: str = Field(min_length=1)  # ISO-8601


class ConversationHistory(BaseModel):
    model_config = _INTERNAL

    phone: str
    available: bool
    messages: list[HistoryMessage] = Field(default_factory=list)
    total_messages: int = 0


class ClientProfile(BaseModel):
    model_config = _INTERNAL

    phone: str
    name: str | None = None
    push_name: str | None = None
    status: str  # open, win, loss, block
    tags: list[str] = Field(default_factory=list)
    last_intent: str | None = None
    last_seen: str | None = None  # ISO-8601
    first_seen: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    total_conversations: int = 0
    total_messages: int = 0


_CONVERSATION_HISTORY_SQL = _sql_text(
    """
    /* agent.conversation_history */
    SELECT
        direction,
        text_content,
        received_at
    FROM genius_next.events
    WHERE tenant_id = :tenant_id
      AND phone = :phone
    ORDER BY received_at DESC
    LIMIT :limit
    """
)

_CLIENT_PROFILE_SQL = _sql_text(
    """
    /* agent.client_profile */
    SELECT
        phone, name, push_name, status, tags, last_intent,
        last_seen, first_seen, metadata
    FROM genius_next.clients
    WHERE tenant_id = :tenant_id
      AND phone = :phone
    """
)

_CLIENT_STATS_SQL = _sql_text(
    """
    /* agent.client_stats */
    SELECT
        COUNT(DISTINCT (provider, instance, remote_jid)) AS total_conversations,
        COUNT(*) AS total_messages
    FROM genius_next.events
    WHERE tenant_id = :tenant_id
      AND phone = :phone
    """
)

_LATEST_MESSAGES_SQL = _sql_text(
    """
    /* agent.latest_messages */
    SELECT
        e.message_id,
        e.direction,
        e.text_content,
        to_char(e.received_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS received_at,
        e.conversation_sequence,
        e.id,
        CASE
            WHEN e.direction = 'inbound' THEN 'customer'
            WHEN o.provider_message_id IS NOT NULL THEN 'genius_ai'
            ELSE 'human_manual'
        END AS sent_by
    FROM genius_next.events e
    LEFT JOIN genius_next.outbox o
        ON o.tenant_id = e.tenant_id
       AND o.provider_message_id = e.message_id
       AND o.provider_message_id IS NOT NULL
    WHERE e.tenant_id = :tenant_id
      AND e.phone = :phone
    ORDER BY e.received_at DESC, e.conversation_sequence DESC, e.id DESC
    LIMIT :limit
    """
)


def get_conversation_history(
    session: Session,
    *,
    tenant_id: str,
    phone: str,
    limit: int = 20,
    max_chars: int = 12000,
    include_team: bool = False,
) -> ConversationHistory:
    """Return the most recent bounded conversation messages for one customer.

    Messages are returned in chronological order (oldest first) for prompt
    injection.  Total character budget is enforced per-message, keeping
    only complete messages that fit within the budget.
    """
    rows = (
        session.execute(
            _LATEST_MESSAGES_SQL,
            {"tenant_id": tenant_id, "phone": phone, "limit": limit},
        )
        .mappings()
        .all()
    )
    if not rows:
        return ConversationHistory(phone=phone, available=False)

    messages: list[HistoryMessage] = []
    remaining = max_chars
    for row in reversed(rows):  # chronological: oldest first
        team_message = row["sent_by"] == "human_manual"
        if team_message and not include_team:
            # Funcionario digitando pelo app oficial ou pelo app do Genius (fromMe=true /
            # envio humano sem provider_message_id no outbox). Com include_team=False
            # (padrao da funcao) nunca entra no prompt do LLM: fecha o vetor de injecao.
            continue
        text = (row["text_content"] or "").strip()
        if not text:
            continue
        if team_message:
            # 08/out/2026 (ordem do chairman): a IA precisa saber o que a equipe disse para
            # nao perder o fio. O prefixo marca como fala da equipe, nunca como instrucao.
            text = f"[equipe] {text}"
        bounded = text[:remaining].rstrip()
        if not bounded:
            break
        messages.append(
            HistoryMessage(
                message_id=row["message_id"],
                role="assistant" if row["direction"] == "outbound" else "customer",
                text=bounded,
                received_at=row["received_at"],
            )
        )
        remaining -= len(bounded)
        if remaining <= 0:
            break

    # Count total messages for this customer
    total = session.execute(
        _sql_text(
            "SELECT COUNT(*) FROM genius_next.events WHERE tenant_id=:tid AND phone=:ph"
        ),
        {"tid": tenant_id, "ph": phone},
    ).scalar_one()

    return ConversationHistory(
        phone=phone,
        available=len(messages) > 0,
        messages=messages,
        total_messages=total,
    )


def get_client_profile(
    session: Session,
    *,
    tenant_id: str,
    phone: str,
) -> ClientProfile | None:
    """Return the full client profile including metadata and aggregate stats."""
    row = (
        session.execute(
            _CLIENT_PROFILE_SQL,
            {"tenant_id": tenant_id, "phone": phone},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None

    stats = session.execute(
        _CLIENT_STATS_SQL,
        {"tenant_id": tenant_id, "phone": phone},
    ).mappings().first()

    return ClientProfile(
        phone=row["phone"],
        name=row["name"],
        push_name=row["push_name"],
        status=row["status"],
        tags=list(row["tags"] or []),
        last_intent=row["last_intent"],
        last_seen=(
            row["last_seen"].strftime("%Y-%m-%dT%H:%M:%SZ")
            if row["last_seen"] else None
        ),
        first_seen=(
            row["first_seen"].strftime("%Y-%m-%dT%H:%M:%SZ")
            if row["first_seen"] else None
        ),
        metadata=dict(row["metadata"] or {}),
        total_conversations=stats["total_conversations"] if stats else 0,
        total_messages=stats["total_messages"] if stats else 0,
    )
