"""Zernio (WhatsApp Cloud API) inbox webhook boundary.

Untrusted ``message.received`` / ``message.sent`` payloads are validated here
and reduced to the same Evolution-shaped envelope the durable pipeline already
consumes, so persistence, debounce, turns and outbox stay provider-agnostic.

Media (08/out/2026): an inbound image, video, audio or file becomes the
Evolution-shaped equivalent (``imageMessage``, ``videoMessage``,
``audioMessage``, ``documentMessage``) plus a ``data.geniusMedia`` reference
(provider media id, kind, mime, sha256, voice, filename). The attachment URL is
never modelled, stored or used: the bytes are fetched later, in a durable job,
from ``ZERNIO_API_URL`` + the media id (app/media/fetch.py).

Known limits (kept explicit on purpose):
  * the caption (``message.text``) and the filename are read from fields that
    the provider docs do not pin; both are optional;
  * a sticker or a shared link alone is ignored (``unsupported_attachment``);
  * a sender with no phone number (WhatsApp username / BSUID only) cannot be
    mapped to a phone identity and is ignored;
  * the payload ``conversation`` block is only partially documented, so every
    field read from it is optional.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import AliasChoices, BaseModel, BeforeValidator, ConfigDict, Field

from app.contracts import MEDIA_ID_PATTERN, EvolutionWebhook

_EXTERNAL = ConfigDict(extra="ignore", populate_by_name=True, str_strip_whitespace=True)
_INTERNAL = ConfigDict(extra="forbid", str_strip_whitespace=True)

_PHONE = re.compile(r"^\+?([1-9][0-9]{4,31})$")
_TEXT_LIMIT = 4_096
_MEDIA_ID = re.compile(MEDIA_ID_PATTERN)
# Tipo de anexo do Zernio -> (kind interno, chave Evolution, messageType).
_MEDIA_ATTACHMENTS: dict[str, tuple[str, str]] = {
    "image": ("image", "imageMessage"),
    "video": ("video", "videoMessage"),
    "audio": ("audio", "audioMessage"),
    "file": ("document", "documentMessage"),
    "document": ("document", "documentMessage"),
}
SUPPORTED_EVENTS = frozenset({"message.received", "message.sent"})


def _stringify(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value)
    return value


_Ident = Annotated[str | None, BeforeValidator(_stringify)]


class ZernioEventIgnored(Exception):
    """A well-formed event the bridge deliberately does not turn into a message."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ZernioSender(BaseModel):
    model_config = _EXTERNAL

    id: _Ident = None
    name: str | None = Field(default=None, max_length=256)
    phone_number: _Ident = Field(default=None, alias="phoneNumber")


def _loose_ident(value: Any) -> Any:
    """Id de midia fora do formato vira None: o evento segue (fallback honesto)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    return value if isinstance(value, str) else None


class ZernioAttachmentPayload(BaseModel):
    model_config = _EXTERNAL

    id: Annotated[str | None, BeforeValidator(_loose_ident)] = None
    mime_type: str | None = Field(default=None, alias="mimeType")
    sha256: str | None = None
    voice: bool | None = None
    file_name: str | None = Field(
        default=None, validation_alias=AliasChoices("filename", "fileName")
    )


class ZernioAttachment(BaseModel):
    """Anexo do Zernio. A ``url`` e deliberadamente NAO modelada (nunca usada)."""

    model_config = _EXTERNAL

    type: str | None = None
    mime_type: str | None = Field(default=None, alias="mimeType")
    file_name: str | None = Field(
        default=None, validation_alias=AliasChoices("filename", "fileName")
    )
    payload: ZernioAttachmentPayload | None = None

    @property
    def effective_mime(self) -> str | None:
        return self.mime_type or (self.payload.mime_type if self.payload else None)

    @property
    def effective_file_name(self) -> str | None:
        return self.file_name or (self.payload.file_name if self.payload else None)


class ZernioMessage(BaseModel):
    model_config = _EXTERNAL

    id: str = Field(min_length=1, max_length=512)
    conversation_id: str = Field(alias="conversationId", min_length=1, max_length=512)
    platform: str = Field(min_length=1, max_length=64)
    platform_message_id: _Ident = Field(default=None, alias="platformMessageId")
    direction: str | None = None
    text: str | None = None
    attachments: list[ZernioAttachment] = Field(default_factory=list)
    sender: ZernioSender | None = None
    sent_via: str | None = Field(default=None, alias="sentVia")


class ZernioConversation(BaseModel):
    model_config = _EXTERNAL

    id: _Ident = None
    participant_id: _Ident = Field(default=None, alias="participantId")
    participant_name: str | None = Field(
        default=None, alias="participantName", max_length=256
    )


class ZernioAccount(BaseModel):
    model_config = _EXTERNAL

    account_id: _Ident = Field(default=None, alias="accountId")
    id: _Ident = None

    @property
    def resolved_id(self) -> str | None:
        return self.account_id or self.id


class ZernioWebhook(BaseModel):
    """Supported subset of the Zernio inbox webhook envelope."""

    model_config = _EXTERNAL

    id: str = Field(min_length=1, max_length=255)
    event: str = Field(min_length=1, max_length=128)
    message: ZernioMessage
    conversation: ZernioConversation | None = None
    account: ZernioAccount
    metadata: dict[str, Any] | None = None


class ZernioIngress(BaseModel):
    """Validated, tenant-free result of normalising one Zernio message event."""

    model_config = _INTERNAL

    account_id: str = Field(min_length=1, max_length=128)
    zernio_conversation_id: str = Field(min_length=1, max_length=512)
    remote_jid: str = Field(min_length=1, max_length=512)
    webhook: EvolutionWebhook


def phone_digits(value: str | None) -> str | None:
    """Digits of an E.164 / bare phone, or ``None`` for BSUIDs and other ids."""
    if not value:
        return None
    match = _PHONE.fullmatch(value.strip())
    return match.group(1) if match else None


def normalize_zernio_event(event: ZernioWebhook) -> ZernioIngress:
    """Map a Zernio message event onto the internal Evolution-shaped envelope.

    Raises ``ZernioEventIgnored`` for events that must not create a message and
    ``ValueError`` for a supported event that lacks identity the pipeline needs.
    """
    if event.event not in SUPPORTED_EVENTS:
        raise ZernioEventIgnored("unsupported_event")
    message = event.message
    if message.platform.casefold() != "whatsapp":
        raise ZernioEventIgnored("unsupported_platform")
    account_id = event.account.resolved_id
    if not account_id:
        raise ValueError("Zernio event has no account id")
    conversation_id = message.conversation_id

    text = (message.text or "").strip()[:_TEXT_LIMIT]
    media = next(
        (
            attachment
            for attachment in message.attachments
            if (attachment.type or "").casefold() in _MEDIA_ATTACHMENTS
        ),
        None,
    )
    if not text and media is None:
        raise ZernioEventIgnored(
            "no_text" if not message.attachments else "unsupported_attachment"
        )
    if event.event == "message.sent" and media is not None and not text:
        raise ZernioEventIgnored("outbound_media_without_text")
    message_id = message.platform_message_id or message.id
    conversation = event.conversation

    if event.event == "message.received":
        if (message.direction or "").casefold() == "outgoing":
            raise ZernioEventIgnored("outgoing_on_received")
        if (event.metadata or {}).get("standby") is True:
            raise ZernioEventIgnored("standby_thread")
        sender = message.sender or ZernioSender()
        digits = (
            phone_digits(sender.phone_number)
            or phone_digits(sender.id)
            or phone_digits(conversation.participant_id if conversation else None)
        )
        if digits is None:
            raise ZernioEventIgnored("sender_without_phone")
        push_name = sender.name or (conversation.participant_name if conversation else None)
        event_name, from_me = "messages.upsert", False
    else:
        if (message.sent_via or "").casefold() == "api":
            raise ZernioEventIgnored("own_api_send")
        digits = phone_digits(conversation.participant_id if conversation else None)
        if digits is None:
            raise ZernioEventIgnored("participant_without_phone")
        push_name = None
        event_name, from_me = "send.message", True

    remote_jid = f"{digits}@s.whatsapp.net"
    data: dict[str, Any] = {
        "key": {"id": message_id, "remoteJid": remote_jid, "fromMe": from_me},
        "message": {"conversation": text},
        "messageType": "conversation",
    }
    if media is not None and event.event == "message.received":
        data.update(_media_envelope(media, text))
    if push_name:
        data["pushName"] = push_name
    return ZernioIngress(
        account_id=account_id,
        zernio_conversation_id=conversation_id,
        remote_jid=remote_jid,
        webhook=EvolutionWebhook.model_validate(
            {"event": event_name, "instance": account_id, "data": data}
        ),
    )


def _clip(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value[:limit] or None


def _media_envelope(attachment: ZernioAttachment, text: str) -> dict[str, Any]:
    """Corpo Evolution-equivalente + ``geniusMedia`` para um anexo inbound.

    Nunca levanta por causa de um campo de midia fora do formato: um id invalido
    vira ``mediaId: None`` (o turno cai no fallback honesto), sem 422.
    """
    kind, body_key = _MEDIA_ATTACHMENTS[(attachment.type or "").casefold()]
    payload = attachment.payload or ZernioAttachmentPayload()
    mime = _clip(attachment.effective_mime, 128)
    file_name = _clip(attachment.effective_file_name, 256)
    media_id = payload.id if payload.id and _MEDIA_ID.fullmatch(payload.id) else None
    body: dict[str, Any] = {}
    if mime:
        body["mimetype"] = mime
    message: dict[str, Any]
    if kind == "audio":
        if payload.voice is not None:
            body["ptt"] = payload.voice
        message = {"audioMessage": body}
        if text:
            message["conversation"] = text
    else:
        if text:
            body["caption"] = text
        if kind == "document" and file_name:
            body["fileName"] = file_name
        message = {body_key: body}
    sha256 = _clip(payload.sha256, 128)
    genius_media: dict[str, Any] = {"mediaId": media_id, "kind": kind}
    if mime:
        genius_media["mimetype"] = mime
    if sha256:
        genius_media["sha256"] = sha256
    if payload.voice is not None:
        genius_media["voice"] = payload.voice
    if file_name:
        genius_media["fileName"] = file_name
    return {"message": message, "messageType": body_key, "geniusMedia": genius_media}
