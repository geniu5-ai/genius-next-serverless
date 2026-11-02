"""Zernio conversationId store: the id the send API needs to answer a contact."""

from __future__ import annotations

from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.durable.tenant_registry import get_registry_engine
from app.identity_primitives import brazilian_mobile_jid_variants
from app.settings import get_settings

_UPSERT_SQL = text(
    """
    /* zernio_conversation.upsert */
    INSERT INTO genius_next.zernio_conversations (
        tenant_id, instance, remote_jid, conversation_id, updated_at
    ) VALUES (
        :tenant_id, :instance, :remote_jid, :conversation_id, now()
    )
    ON CONFLICT (tenant_id, instance, remote_jid) DO UPDATE
    SET conversation_id = EXCLUDED.conversation_id,
        updated_at = now()
    """
)

# ``remote_jid IN (variants)``: the same Brazilian handset is stored without the
# ninth digit by Zernio but registered with it (manager_jid, what people dial).
_LOOKUP_SQL = text(
    """
    /* zernio_conversation.lookup */
    SELECT conversation_id
    FROM genius_next.zernio_conversations
    WHERE tenant_id = :tenant_id
      AND instance = :instance
      AND remote_jid IN :remote_jids
    ORDER BY updated_at DESC
    LIMIT 1
    """
).bindparams(bindparam("remote_jids", expanding=True))

# Route of a contact for an enabled Zernio tenant: which Zernio account (instance)
# and which stored remote_jid the conversation lives under.
_ROUTE_SQL = text(
    """
    /* zernio_conversation.route */
    SELECT z.instance, z.remote_jid, z.conversation_id
    FROM genius_next.zernio_conversations z
    JOIN genius_next.tenant_registry r
      ON r.provider = 'zernio'
     AND r.tenant_id = z.tenant_id
     AND r.instance = z.instance
     AND r.enabled = true
    WHERE z.tenant_id = :tenant_id
      AND z.remote_jid IN :remote_jids
    ORDER BY z.updated_at DESC
    LIMIT 1
    """
).bindparams(bindparam("remote_jids", expanding=True))


def upsert_zernio_conversation(
    *,
    tenant_id: str,
    instance: str,
    remote_jid: str,
    conversation_id: str,
    database_url: str | None = None,
) -> None:
    """Idempotently remember the Zernio conversationId for one trusted contact."""
    url = database_url or get_settings().DATABASE_URL
    with Session(get_registry_engine(url)) as session, session.begin():
        session.execute(
            _UPSERT_SQL,
            {
                "tenant_id": tenant_id,
                "instance": instance,
                "remote_jid": remote_jid,
                "conversation_id": conversation_id,
            },
        )


def lookup_zernio_conversation(
    *,
    tenant_id: str,
    instance: str,
    remote_jid: str,
    database_url: str | None = None,
) -> str | None:
    """Return the stored Zernio conversationId for a contact, if any.

    Brazilian mobile numbers match with or without the ninth digit; when both
    rows exist the most recently updated one wins.
    """
    url = database_url or get_settings().DATABASE_URL
    with Session(get_registry_engine(url)) as session:
        return session.execute(
            _LOOKUP_SQL,
            {
                "tenant_id": tenant_id,
                "instance": instance,
                "remote_jids": list(brazilian_mobile_jid_variants(remote_jid)),
            },
        ).scalar_one_or_none()


def find_zernio_route(
    *,
    tenant_id: str,
    remote_jid: str,
    database_url: str | None = None,
) -> dict[str, Any] | None:
    """Return ``{instance, remote_jid, conversation_id}`` for a contact, if any.

    Only conversations whose Zernio account is an enabled ``tenant_registry`` row
    of the tenant count.  Same ninth-digit variants and recency rule as
    :func:`lookup_zernio_conversation`.
    """
    url = database_url or get_settings().DATABASE_URL
    with Session(get_registry_engine(url)) as session:
        row = session.execute(
            _ROUTE_SQL,
            {
                "tenant_id": tenant_id,
                "remote_jids": list(brazilian_mobile_jid_variants(remote_jid)),
            },
        ).mappings().first()
    return dict(row) if row is not None else None
