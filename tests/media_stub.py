"""Dublê de ``enrich_turn_media`` para testes que não exercitam a rota de mídia."""

from __future__ import annotations

from typing import Any

from app.durable.media import TurnMedia


def passthrough_media(turn: Any, **_kwargs: Any) -> TurnMedia:
    """Segue sem mexer no turno; ``safety_texts`` espelha o que a rota real extrai do texto."""
    texts = [
        event.text_content
        for event in getattr(turn, "events", None) or []
        if getattr(event, "direction", None) == "inbound"
        and isinstance(getattr(event, "text_content", None), str)
        and event.text_content.strip()
    ]
    return TurnMedia(turn=turn, outcome="proceed", safety_texts=texts)
