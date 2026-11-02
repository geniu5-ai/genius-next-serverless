"""Exact, tenant-scoped commercial prompt-profile resolution."""

from __future__ import annotations

import logging
from typing import Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.durable.tenant_registry import get_registry_engine
from app.settings import get_settings

PromptPurpose = Literal[
    "reply_normal", "reply_mini", "followup", "persuasao", "guardrail", "criticas",
    "manager_triage",
]
_PURPOSES = frozenset(
    {
        "reply_normal", "reply_mini", "followup", "persuasao", "guardrail",
        "criticas", "manager_triage",
    }
)
logger = logging.getLogger(__name__)


def _resolve_template_tenant(session: Session, tenant_id: str) -> str:
    """If tenant_id is a live/test slot bound to a template, resolve to the template.

    21/set/2026: tenant_prompt_binding (migration 062) exists so a live tenant
    (the one actually wired to an Evolution webhook) never stores prompt text
    of its own again -- it only points at a template tenant_id. Switching
    which vertical is under test becomes one UPDATE on the binding row, never
    an INSERT that overwrites another vertical's prompt in place (root cause
    of the odonto/advocacia mix-up found this session).
    """
    row = session.execute(
        text(
            "SELECT template_tenant_id FROM genius_next.tenant_prompt_binding "
            "WHERE live_tenant_id = :tenant_id"
        ),
        {"tenant_id": tenant_id},
    ).first()
    return str(row[0]) if row is not None else tenant_id


def active_prompt_for(tenant_id: str, purpose: PromptPurpose | str) -> str | None:
    """Return only this tenant and purpose's active profile; never cross-fallback."""
    if not tenant_id.strip():
        raise ValueError("tenant_id must not be empty")
    if purpose not in _PURPOSES:
        raise ValueError(f"unsupported prompt purpose: {purpose}")
    try:
        settings = get_settings()
        database_url = settings.PROMPT_DATABASE_URL or settings.DATABASE_URL
        with Session(get_registry_engine(database_url)) as session:
            resolved_tenant_id = _resolve_template_tenant(session, tenant_id)
            row = session.execute(
                text(
                    "SELECT prompt FROM genius_next.tenant_prompt_profiles "
                    "WHERE tenant_id = :tenant_id AND purpose = :purpose "
                    "AND is_active = true ORDER BY version DESC LIMIT 1"
                ),
                {"tenant_id": resolved_tenant_id, "purpose": purpose},
            ).first()
        return None if row is None else str(row[0])
    except Exception:  # table is absent until the additive migration is applied
        logger.warning("tenant prompt profile load failed", exc_info=True)
        return None


def tenant_rules(tenant_id: str, purpose: PromptPurpose) -> list[str]:
    """Regras de um tenant, uma por linha, vindas do Postgres.

    Fase 3 da SPEC_PYDANTIC_AI: persuasao e guardrail saiam do Python e passem a
    viver ao lado do prompt, por tenant.  Estavam hardcoded em app/reply/llm.py
    e contradiziam o prompt do tenant sem aparecer em lugar nenhum que o
    operador olhasse.  Lista vazia quando o tenant nao tem linha — quem chama
    decide o fallback.
    """
    texto = active_prompt_for(tenant_id, purpose)
    if not texto:
        return []
    return [linha.strip() for linha in texto.splitlines() if linha.strip()]
