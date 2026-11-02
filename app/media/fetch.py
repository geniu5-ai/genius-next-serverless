"""Download de midia inbound (Zernio e Evolution) — bytes so em memoria.

Nada aqui toca disco nem loga conteudo. A URL do Zernio e montada SO a partir
de ``ZERNIO_API_URL`` + id da midia (nunca a url do payload: evita SSRF e
vazamento do Bearer). Falha permanente vira ``MediaFailure``; falha transitoria
(429, 5xx, timeout, transporte) levanta ``MediaTransientError`` para o job
tentar de novo.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Callable, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

MediaKind = Literal["image", "audio", "video", "document"]


class MediaTransientError(RuntimeError):
    """Falha que vale nova tentativa (o job repete)."""


class MediaJob(BaseModel):
    """O que o workflow de midia precisa para baixar e extrair um arquivo."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=128)
    provider: Literal["zernio", "evolution"]
    instance: str = Field(min_length=1, max_length=128)
    message_id: str = Field(min_length=1, max_length=512)
    kind: MediaKind
    mimetype: str | None = Field(default=None, max_length=128)
    media_id: str | None = Field(default=None, max_length=128)
    sha256: str | None = Field(default=None, max_length=128)
    voice: bool = False
    file_name: str | None = Field(default=None, max_length=256)


@dataclass(frozen=True)
class MediaBlob:
    data: bytes
    mimetype: str

    def __repr__(self) -> str:  # nunca despeja bytes em log/traceback
        return f"MediaBlob(mimetype={self.mimetype!r}, size={len(self.data)})"


@dataclass(frozen=True)
class MediaFailure:
    reason: str
    size_bytes: int | None = None
    mimetype: str | None = None


ALLOWED_MIME: dict[str, frozenset[str]] = {
    "image": frozenset({"image/jpeg", "image/png", "image/webp"}),
    "audio": frozenset(
        {
            "audio/ogg",
            "audio/mpeg",
            "audio/mp4",
            "audio/aac",
            "audio/wav",
            "audio/x-wav",
            "audio/flac",
            "audio/amr",
        }
    ),
    "video": frozenset({"video/mp4", "video/3gpp"}),
    "document": frozenset({"application/pdf", "text/plain", "image/jpeg", "image/png"}),
}

_MIME_ALIASES = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "audio/opus": "audio/ogg",
    "audio/mp3": "audio/mpeg",
    "audio/x-m4a": "audio/mp4",
    "audio/m4a": "audio/mp4",
    "audio/wave": "audio/wav",
    "audio/x-flac": "audio/flac",
}

_GENERIC_MIME = frozenset({"", "application/octet-stream", "binary/octet-stream"})


def normalize_mime(value: str | None) -> str:
    """``audio/ogg; codecs=opus`` -> ``audio/ogg`` (minusculas, com aliases)."""
    if not value:
        return ""
    base = value.split(";", 1)[0].strip().lower()
    return _MIME_ALIASES.get(base, base)


def max_bytes_for(kind: str, settings: Any) -> int:
    return {
        "image": settings.MEDIA_MAX_BYTES_IMAGE,
        "audio": settings.MEDIA_MAX_BYTES_AUDIO,
        "video": settings.MEDIA_MAX_BYTES_VIDEO,
        "document": settings.MEDIA_MAX_BYTES_DOCUMENT,
    }[kind]


def _magic_ok(mime: str, data: bytes) -> bool:
    """Confere a assinatura do arquivo com o mime declarado."""
    if mime == "image/jpeg":
        return data[:3] == b"\xff\xd8\xff"
    if mime == "image/png":
        return data[:4] == b"\x89PNG"
    if mime == "image/webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if mime == "application/pdf":
        return data[:5] == b"%PDF-"
    if mime == "audio/ogg":
        return data[:4] == b"OggS"
    if mime in {"video/mp4", "video/3gpp", "audio/mp4"}:
        return data[4:8] == b"ftyp"
    if mime in {"audio/wav", "audio/x-wav"}:
        return data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    if mime == "audio/flac":
        return data[:4] == b"fLaC"
    if mime == "audio/amr":
        return data[:5] == b"#!AMR"
    # audio/mpeg, audio/aac e text/plain nao tem assinatura fixa confiavel.
    return True


def _integrity_ok(expected: str | None, data: bytes) -> bool:
    if not expected:
        return True
    digest = hashlib.sha256(data).digest()
    candidate = expected.strip()
    if candidate.lower() == digest.hex():
        return True
    try:
        return base64.b64decode(candidate, validate=False) == digest
    except (binascii.Error, ValueError):
        return False


def validate_blob(
    job: MediaJob, data: bytes, declared_mime: str | None, settings: Any
) -> MediaBlob | MediaFailure:
    """Teto, allowlist, magic bytes e integridade — comum a Zernio e Evolution."""
    size = len(data)
    if size == 0:
        return MediaFailure("empty", size_bytes=0)
    if size > max_bytes_for(job.kind, settings):
        return MediaFailure("too_large", size_bytes=size)
    mime = normalize_mime(declared_mime)
    if mime in _GENERIC_MIME:
        mime = normalize_mime(job.mimetype)
    if mime not in ALLOWED_MIME[job.kind]:
        return MediaFailure("unsupported", size_bytes=size, mimetype=mime or None)
    if not _magic_ok(mime, data):
        return MediaFailure("mime_mismatch", size_bytes=size, mimetype=mime)
    if not _integrity_ok(job.sha256, data):
        return MediaFailure("integrity", size_bytes=size, mimetype=mime)
    return MediaBlob(data=data, mimetype=mime)


def zernio_media_url(settings: Any, media_id: str, account_id: str) -> str:
    """Unica forma de montar a URL: base configurada + id (nunca a url do payload)."""
    base = str(settings.ZERNIO_API_URL).rstrip("/")
    return (
        f"{base}/v1/whatsapp/media/{quote(media_id, safe='')}"
        f"?accountId={quote(account_id, safe='')}"
    )


def fetch_zernio_media(
    job: MediaJob, settings: Any, client: httpx.Client | None = None
) -> MediaBlob | MediaFailure:
    """Baixa a midia pelo endpoint autenticado do Zernio, com teto em streaming."""
    if not job.media_id:
        return MediaFailure("no_media_id")
    api_key = getattr(settings, "ZERNIO_API_KEY", None)
    secret = api_key.get_secret_value() if api_key is not None else ""
    if not secret:
        return MediaFailure("auth")
    limit = max_bytes_for(job.kind, settings)
    url = zernio_media_url(settings, job.media_id, job.instance)
    owned = client is None
    http = client or httpx.Client()
    try:
        with http.stream(
            "GET",
            url,
            headers={"Authorization": f"Bearer {secret}"},
            follow_redirects=False,
            timeout=settings.MEDIA_FETCH_TIMEOUT_SECONDS,
        ) as response:
            status = response.status_code
            if 300 <= status < 400:
                return MediaFailure("redirect")
            if status == 400:
                return MediaFailure("expired")
            if status in (401, 403):
                logger.error("zernio media fetch unauthorized (status=%s)", status)
                return MediaFailure("auth")
            if status == 404:
                return MediaFailure("not_found")
            if status == 429 or status >= 500:
                raise MediaTransientError(f"zernio media status {status}")
            if status != 200:
                return MediaFailure("not_found")
            declared = response.headers.get("content-length")
            if declared is not None and declared.strip().isdigit() and int(declared) > limit:
                return MediaFailure("too_large", size_bytes=int(declared))
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > limit:
                    return MediaFailure("too_large", size_bytes=total)
                chunks.append(chunk)
            data = b"".join(chunks)
            content_type = response.headers.get("content-type")
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise MediaTransientError(f"zernio media transport: {type(exc).__name__}") from exc
    finally:
        if owned:
            http.close()
    return validate_blob(job, data, content_type, settings)


InlineLoader = Callable[[MediaJob, Any], "str | None"]


def load_evolution_inline_base64(job: MediaJob, settings: Any) -> str | None:
    """Le o base64 que veio no proprio evento, direto do banco (dentro do job).

    Lido aqui, e nao numa transacao do workflow, para que os bytes nunca sejam
    persistidos junto com o job.
    """
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from app.durable.tenant_registry import get_registry_engine

    sql = text(
        """
        /* event_media.inline_base64 */
        SELECT COALESCE(
            raw_payload #>> '{data,message,audioMessage,base64}',
            raw_payload #>> '{data,message,base64}'
        )
        FROM genius_next.events
        WHERE tenant_id = :tenant_id
          AND provider = :provider
          AND message_id = :message_id
          AND direction = 'inbound'
        """
    )
    with Session(get_registry_engine(settings.DATABASE_URL)) as session:
        return session.execute(
            sql,
            {
                "tenant_id": job.tenant_id,
                "provider": job.provider,
                "message_id": job.message_id,
            },
        ).scalar_one_or_none()


def _decode_b64(value: str, limit: int) -> bytes | None:
    if len(value) > (limit * 4) // 3 + 8:
        return None
    try:
        return base64.b64decode(value, validate=False)
    except (binascii.Error, ValueError):
        return b""


def fetch_evolution_media(
    job: MediaJob,
    settings: Any,
    client: httpx.Client | None = None,
    *,
    inline_loader: InlineLoader | None = None,
) -> MediaBlob | MediaFailure:
    """Base64 inline do evento primeiro; senao ``getBase64FromMediaMessage``."""
    limit = max_bytes_for(job.kind, settings)
    loader = inline_loader or load_evolution_inline_base64
    inline = loader(job, settings)
    if inline:
        data = _decode_b64(inline, limit)
        if data is None:
            return MediaFailure("too_large")
        return validate_blob(job, data, job.mimetype, settings)
    if not getattr(settings, "MEDIA_EVOLUTION_FETCH_ENABLED", False):
        return MediaFailure("fetch_disabled")
    base_url = getattr(settings, "EVOLUTION_URL", None)
    api_key = getattr(settings, "EVOLUTION_API_KEY", None)
    if not base_url or api_key is None:
        return MediaFailure("auth")
    url = (
        f"{str(base_url).rstrip('/')}/chat/getBase64FromMediaMessage/"
        f"{quote(job.instance, safe='')}"
    )
    body = {"message": {"key": {"id": job.message_id}}, "convertToMp4": False}
    response_cap = int(limit * 1.4) + 4_096
    owned = client is None
    http = client or httpx.Client()
    try:
        with http.stream(
            "POST",
            url,
            headers={"apikey": api_key.get_secret_value()},
            json=body,
            follow_redirects=False,
            timeout=settings.MEDIA_FETCH_TIMEOUT_SECONDS,
        ) as response:
            status = response.status_code
            if 300 <= status < 400:
                return MediaFailure("redirect")
            if status in (401, 403):
                logger.error("evolution media fetch unauthorized (status=%s)", status)
                return MediaFailure("auth")
            if status == 429 or status >= 500:
                raise MediaTransientError(f"evolution media status {status}")
            if status == 404:
                return MediaFailure("not_found")
            if status not in (200, 201):
                return MediaFailure("expired")
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > response_cap:
                    return MediaFailure("too_large", size_bytes=total)
                chunks.append(chunk)
            raw = b"".join(chunks)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise MediaTransientError(
            f"evolution media transport: {type(exc).__name__}"
        ) from exc
    finally:
        if owned:
            http.close()
    try:
        import json

        parsed = json.loads(raw)
    except ValueError:
        return MediaFailure("unsupported")
    if not isinstance(parsed, dict) or not isinstance(parsed.get("base64"), str):
        return MediaFailure("unsupported")
    data = _decode_b64(parsed["base64"], limit)
    if data is None:
        return MediaFailure("too_large")
    mime = parsed.get("mimetype") if isinstance(parsed.get("mimetype"), str) else None
    return validate_blob(job, data, mime or job.mimetype, settings)


def fetch_media(
    job: MediaJob, settings: Any, client: httpx.Client | None = None
) -> MediaBlob | MediaFailure:
    if job.provider == "zernio":
        return fetch_zernio_media(job, settings, client)
    return fetch_evolution_media(job, settings, client)
