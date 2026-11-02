"""Historico da conversa normal, lido do Postgres — nunca da Evolution.

A leitura ao vivo em ``/chat/findMessages`` continua existindo em
``app.ingress.evolution.fetch_history`` e passa a ser material do **Genius
Rescue**: resgatar, resumir e curar conversa antiga e' outra feature, com outro
ciclo de vida.  O turno normal usa so' o que o proprio Genius Next ja' gravou em
``genius_next.events`` — uma linha por mensagem do cliente e uma por resposta da
IA — que e' o comportamento que o n8n tinha e o que o chairman pediu em
25/ago/2026.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.durable.persistence import ClaimedTurn
from app.durable.tenant_registry import get_registry_engine
from app.agent.memory import get_conversation_history
from app.identity_primitives import normalize_phone
from app.ingress.evolution import HistoryMessage, HistoryResult
from app.settings import get_settings

logger = logging.getLogger(__name__)


def _epoch(received_at: str) -> int:
    """ISO-8601 -> epoch; a ordenacao do merge em reply/llm depende disso."""
    try:
        return int(datetime.fromisoformat(received_at.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def fetch_pg_history_step(turn: ClaimedTurn) -> HistoryResult:
    """Checkpoint o historico da conversa lido do Postgres para um turno."""
    settings = get_settings()
    try:
        # events.phone guarda a parte local normalizada, nao o JID completo.
        phone = normalize_phone(turn.remote_jid)
    except ValueError:
        return HistoryResult.unavailable()

    # As mensagens deste turno chegam ao prompt por outro caminho; repeti-las
    # aqui faria a IA responder ao proprio recorte.
    excluded = {event.message_id for event in turn.events}

    try:
        with Session(get_registry_engine(settings.DATABASE_URL)) as session:
            history = get_conversation_history(
                session,
                tenant_id=turn.tenant_id,
                phone=phone,
                limit=settings.HISTORY_MAX_MESSAGES + len(turn.events),
                max_chars=settings.HISTORY_TOTAL_MAX_CHARS,
                include_team=settings.HISTORY_INCLUDE_TEAM_MESSAGES,
            )
    except Exception:
        logger.warning("pg history fetch failed", exc_info=True)
        return HistoryResult.unavailable()

    messages = [
        HistoryMessage(
            message_id=message.message_id,
            role=message.role,
            text=message.text,
            timestamp=_epoch(message.received_at),
        )
        for message in history.messages
        if message.message_id and message.message_id not in excluded
    ]
    # Mantem as mais recentes dentro do teto do contrato (max_length=20).
    messages = messages[-settings.HISTORY_MAX_MESSAGES :]
    return HistoryResult(available=bool(messages), messages=messages)
