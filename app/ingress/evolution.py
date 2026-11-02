"""Bounded read-only Evolution v2.3.7 history adapter."""

from __future__ import annotations

from typing import Literal

import logging

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.durable.persistence import ClaimedTurn
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)

_EXTERNAL = ConfigDict(extra="ignore", populate_by_name=True, str_strip_whitespace=True)
_INTERNAL = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _HistoryKey(BaseModel):
    model_config = _EXTERNAL

    id: str = Field(min_length=1, max_length=512)
    remote_jid: str = Field(alias="remoteJid", min_length=1, max_length=512)
    from_me: bool = Field(alias="fromMe")


class _HistoryText(BaseModel):
    model_config = _EXTERNAL

    text: str | None = None


class _HistoryCaption(BaseModel):
    model_config = _EXTERNAL

    caption: str | None = None


class _HistoryBody(BaseModel):
    model_config = _EXTERNAL

    conversation: str | None = None
    extended_text: _HistoryText | None = Field(
        default=None, alias="extendedTextMessage"
    )
    image: _HistoryCaption | None = Field(default=None, alias="imageMessage")
    video: _HistoryCaption | None = Field(default=None, alias="videoMessage")

    @property
    def text_content(self) -> str | None:
        candidates = (
            self.conversation,
            self.extended_text.text if self.extended_text else None,
            self.image.caption if self.image else None,
            self.video.caption if self.video else None,
        )
        return next((candidate for candidate in candidates if candidate), None)


class _HistoryRecord(BaseModel):
    model_config = _EXTERNAL

    key: _HistoryKey
    message: _HistoryBody
    timestamp: int = Field(default=0, alias="messageTimestamp")


class _HistoryRecords(BaseModel):
    model_config = _EXTERNAL

    records: list[_HistoryRecord]


class _HistoryResponse(BaseModel):
    model_config = _EXTERNAL

    messages: _HistoryRecords


class HistoryMessage(BaseModel):
    model_config = _INTERNAL

    message_id: str = Field(min_length=1, max_length=512)
    role: Literal["customer", "assistant"]
    text: str = Field(min_length=1, max_length=4_096)
    timestamp: int = Field(default=0, ge=0)


class HistoryResult(BaseModel):
    model_config = _INTERNAL

    available: bool
    messages: list[HistoryMessage] = Field(default_factory=list, max_length=20)

    @classmethod
    def unavailable(cls) -> HistoryResult:
        return cls(available=False, messages=[])


def _secret_value(secret: SecretStr | None) -> str:
    if secret is None:
        raise ValueError("Evolution API key is not configured")
    return secret.get_secret_value()


def fetch_history(
    turn: ClaimedTurn,
    *,
    settings: Settings,
    client: httpx.Client | None = None,
) -> HistoryResult:
    """Fetch, revalidate and bound the most recent supported history records."""
    if settings.EVOLUTION_URL is None:
        return HistoryResult.unavailable()

    owned_client = client is None
    http = client or httpx.Client(timeout=settings.HISTORY_TIMEOUT_SECONDS)
    try:
        response = http.post(
            f"{settings.EVOLUTION_URL}/chat/findMessages/{turn.instance}",
            headers={"apikey": _secret_value(settings.EVOLUTION_API_KEY)},
            json={
                "where": {"key": {"remoteJid": turn.remote_jid}},
                "page": 1,
                "offset": 20,
            },
        )
        if response.status_code != 200:
            return HistoryResult.unavailable()
        if len(response.content) > settings.HISTORY_RESPONSE_MAX_BYTES:
            return HistoryResult.unavailable()
        parsed = _HistoryResponse.model_validate(response.json())
    except Exception:
        logger.warning("history fetch failed", exc_info=True)
        return HistoryResult.unavailable()
    finally:
        if owned_client:
            http.close()

    excluded = {event.message_id for event in turn.events}
    seen: set[str] = set()
    selected_desc: list[HistoryMessage] = []
    remaining = settings.HISTORY_TOTAL_MAX_CHARS
    for record in parsed.messages.records:
        key = record.key
        if key.remote_jid != turn.remote_jid or key.id in excluded or key.id in seen:
            continue
        seen.add(key.id)
        content = record.message.text_content
        if content is None:
            continue
        bounded = content.strip()[: settings.HISTORY_ITEM_MAX_CHARS].strip()
        if not bounded:
            continue
        bounded = bounded[:remaining].rstrip()
        if not bounded:
            break
        selected_desc.append(
            HistoryMessage(
                message_id=key.id,
                role="assistant" if key.from_me else "customer",
                text=bounded,
                timestamp=record.timestamp,
            )
        )
        remaining -= len(bounded)
        if len(selected_desc) >= settings.HISTORY_MAX_MESSAGES or remaining == 0:
            break

    selected_desc.reverse()
    return HistoryResult(available=True, messages=selected_desc)


def fetch_history_step(turn: ClaimedTurn) -> HistoryResult:
    """Checkpoint the sole read-only Evolution history request for one turn."""
    return fetch_history(turn, settings=get_settings())
