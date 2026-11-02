"""Transcription adapter — Groq Whisper Large v3 Turbo (STT).

Porta a entrada de voz do Genius Sell para Python puro e testável,
seguindo o mesmo padrão do adapter TTS (``app/media/tts.py``): o custo/
fornecedor muda por config, o fluxo não muda.

A transcrição NUNCA roda no caminho do webhook: acontece dentro do job do
turno. Falha de transcrição NUNCA quebra o turno —
retorna ``None`` e o chamador decide o fallback humano (portado do node
``Get Audio`` do legado).
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from app.settings import Settings, get_settings

_GROQ_TRANSCRIPTIONS_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


class TranscriptionResult(BaseModel):
    """Texto transcrito, já normalizado para o contexto do turno."""

    model_config = ConfigDict(extra="forbid")

    text: str
    model: str
    duration_seconds: float | None = None


class TranscriptionError(RuntimeError):
    """Falha de transcrição — o chamador decide o fallback (nunca quebrar o turno)."""


class _GroqTranscriptionResponse(BaseModel):
    """Minimal OpenAI-compatible transcription response (extra ignored)."""

    model_config = ConfigDict(extra="ignore")

    text: str


class STTProvider:
    """Adapter STT — hoje só Groq registrado; interface mínima e testável."""

    def transcribe(
        self,
        media_base64: str,
        *,
        settings: Settings,
        client: httpx.Client | None = None,
    ) -> TranscriptionResult:
        """Transcreve áudio base64 via endpoint OpenAI-compatível do Groq.

        Levanta TranscriptionError em qualquer falha — o chamador decide o
        fallback humano, nunca quebra o turno.
        """
        if not media_base64:
            raise TranscriptionError("empty media payload")
        try:
            data = base64.b64decode(media_base64)
        except (binascii.Error, ValueError) as exc:
            raise TranscriptionError("invalid base64") from exc
        return self.transcribe_bytes(
            data,
            mimetype="audio/ogg",
            filename="audio.ogg",
            settings=settings,
            client=client,
        )

    def transcribe_bytes(
        self,
        data: bytes,
        *,
        mimetype: str,
        filename: str,
        settings: Any,
        client: httpx.Client | None = None,
    ) -> TranscriptionResult:
        """Transcreve bytes com o mime e o nome de arquivo reais (rota de midia)."""
        if settings.GROQ_API_KEY is None:
            raise TranscriptionError("missing API key")
        if not data:
            raise TranscriptionError("empty media payload")

        # STT_MODEL aceita uma lista separada por virgula: tenta na ordem e so falha
        # quando todos falharem (19/out/2026: o fluxo de audio nao pode depender de um modelo).
        models = [m.strip() for m in str(settings.STT_MODEL).split(",") if m.strip()]
        last_error = TranscriptionError("no stt model configured")
        for model in models:
            try:
                return self._transcribe_one(
                    data, mimetype=mimetype, filename=filename, model=model,
                    settings=settings, client=client,
                )
            except TranscriptionError as exc:
                last_error = exc
        raise last_error

    def _transcribe_one(
        self,
        data: bytes,
        *,
        mimetype: str,
        filename: str,
        model: str,
        settings: Any,
        client: httpx.Client | None = None,
    ) -> TranscriptionResult:
        owned_client = client is None
        http = client or httpx.Client(timeout=settings.STT_TIMEOUT_SECONDS)
        if model.startswith("cohere/"):
            # Reserva direta na Cohere (20/out/2026): fora do Omni, endpoint proprio e idioma obrigatorio.
            cohere_key = getattr(settings, "COHERE_API_KEY", None)
            if cohere_key is None:
                raise TranscriptionError("missing cohere key")
            url = getattr(settings, "COHERE_STT_URL", "https://api.cohere.com/v2/audio/transcriptions")
            headers = {"Authorization": f"Bearer {cohere_key.get_secret_value()}"}
            form = {"model": model.removeprefix("cohere/"), "language": getattr(settings, "STT_LANGUAGE", "pt")}
            used_model = model
        else:
            url = getattr(settings, "STT_URL", None) or _GROQ_TRANSCRIPTIONS_URL
            headers = {"Authorization": f"Bearer {settings.GROQ_API_KEY.get_secret_value()}"}
            form = {"model": model, "response_format": "json"}
            used_model = model
        try:
            response = http.post(
                url,
                headers=headers,
                files={"file": (filename, data, mimetype)},
                data=form,
            )
        except httpx.TransportError as exc:
            raise TranscriptionError(f"transport error: {exc}") from exc
        finally:
            if owned_client:
                http.close()

        if response.status_code != 200:
            raise TranscriptionError(f"status {response.status_code}")
        if len(response.content) > settings.STT_RESPONSE_MAX_BYTES:
            raise TranscriptionError("response too large")
        try:
            parsed = _GroqTranscriptionResponse.model_validate(response.json())
        except Exception as exc:
            raise TranscriptionError("provider protocol") from exc
        if not parsed.text.strip():
            raise TranscriptionError("empty transcription")
        return TranscriptionResult(text=parsed.text.strip(), model=used_model)


class GroqSTTProvider(STTProvider):
    """Implementação primária — Groq Whisper Large v3 Turbo (LPU, 216x realtime)."""


def make_stt_provider() -> STTProvider:
    """Factory do adapter — hoje só o Groq registrado."""
    return GroqSTTProvider()


def transcribe_audio_step(media_base64: str) -> TranscriptionResult | None:
    """Único caminho runtime de transcrição; ``None`` = caller deve cair no fallback."""
    settings = get_settings()
    if settings.GROQ_API_KEY is None:
        return None
    try:
        provider = make_stt_provider()
        return provider.transcribe(media_base64, settings=settings)
    except TranscriptionError:
        return None
