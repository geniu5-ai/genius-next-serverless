"""TTS adapter — ElevenLabs primário, fallback econômico futuro (Mistral Voxtral / Fish Audio).

Porta o node ``Audio`` do legado n8n (Genius Sell) para Python puro e
testável, e adiciona a síntese via adapter ``TTSProvider`` (mesmo padrão do
``llm_router`` do Genius-Dev): o custo/fornecedor muda por config, o fluxo
não muda.

A sintese TTS nunca roda no caminho do webhook: acontece dentro do job do
turno. Falha de TTS NUNCA quebra o turno — retorna ``None`` e o
chamador decide o fallback.
"""

from __future__ import annotations

import base64
import re
from abc import ABC, abstractmethod

import httpx
from pydantic import BaseModel, ConfigDict

from app.settings import Settings, get_settings

# Voice/accent tag aplicada à mensagem inteira (eleven_v3 creative tag:
# sotaque Nordeste/Ceará). Fiel ao legado — não "melhorar".
_ACCENT_TAG = "[sotaque cearense nativo, natural e conversacional]"

# Mesmos ranges de emoji do node Audio (jsCode) — espelhados 1:1.
_EMOJI_PATTERN = re.compile(
    r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
    r"\U0001F700-\U0001F77F\U0001F780-\U0001F7FF\U0001F800-\U0001F8FF"
    r"\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF"
    r"\u2600-\u26FF\u2700-\u27BF\uFE00-\uFE0F\U0001F1E0-\U0001F1FF]"
)
_MONEY_PATTERN = re.compile(r"R\$\s*(\d+(?:[.,]\d+)?)")
_TRAILING_PERIODS = re.compile(r"\.{3,}")
_REPEATED_QUESTION_EXCLAMATION = re.compile(r"([!?]){2,}")


class TtsText(BaseModel):
    """Variantes normalizadas de um reply, espelhando os outputs do node legado."""

    model_config = ConfigDict(extra="forbid")

    original: str
    flat: str
    with_pauses: str
    city: str | None = None


def normalize_for_tts(text: str, *, city: str | None = None) -> TtsText:
    """Porta do node ``Audio``: emojis fora, formas faladas, pausas naturais.

    ``flat`` é linha única sem marcadores de pausa; ``with_pauses`` mantém o
    prefixo de cidade (sotaque regional) e a accent tag para ``eleven_v3``.
    Espelhar o JavaScript exatamente — não "melhorar".
    """
    raw = text.strip()
    no_emoji = _EMOJI_PATTERN.sub("", raw)
    no_emoji = re.sub(r"\bDra\.", "Doutora", no_emoji)
    no_emoji = re.sub(r"\bDr\.", "Doutor", no_emoji)
    normalized = _MONEY_PATTERN.sub(r"\1 reais", no_emoji).strip()
    flat = normalized.replace("\r", "")
    flat = re.sub(r"\n+", " ", flat)
    flat = re.sub(r"\s+", " ", flat).strip()
    with_pauses = normalized.replace("\r", "")
    with_pauses = re.sub(r"\n{2,}", ". ", with_pauses)
    with_pauses = re.sub(r"\n+", " ", with_pauses)
    with_pauses = _TRAILING_PERIODS.sub("...", with_pauses)
    with_pauses = _REPEATED_QUESTION_EXCLAMATION.sub(r"\1", with_pauses)
    with_pauses = re.sub(r"\s+", " ", with_pauses).strip()
    if city:
        with_pauses = f"Você aí de {city}… " + with_pauses
    with_pauses = f"{_ACCENT_TAG} {with_pauses}"
    return TtsText(original=raw, flat=flat, with_pauses=with_pauses, city=city)


class TtsResult(BaseModel):
    """Áudio sintetizado, pronto para o send-audio da Evolution."""

    model_config = ConfigDict(extra="forbid")

    media_base64: str
    mime_type: str = "audio/mpeg"
    chars: int = 0


class TtsError(RuntimeError):
    """Falha de síntese — o chamador decide o fallback (nunca quebrar o turno)."""


class TTSProvider(ABC):
    """Interface do adapter: desacopla custo/fornecedor sem tocar no fluxo."""

    @abstractmethod
    def synthesize(
        self,
        text: str,
        *,
        settings: Settings,
        client: httpx.Client | None = None,
    ) -> TtsResult:
        """Sintetiza texto em áudio base64. Levanta TtsError em falha."""


class ElevenLabsTTS(TTSProvider):
    """Implementação primária — mesma voz/parâmetros do node "Eleven Labs" do n8n."""

    def synthesize(
        self,
        text: str,
        *,
        settings: Settings,
        client: httpx.Client | None = None,
    ) -> TtsResult:
        if len(text) > settings.TTS_INPUT_MAX_CHARS:
            raise TtsError("text too long")
        if settings.ELEVENLABS_API_KEY is None:
            raise TtsError("missing API key")
        if not settings.ELEVENLABS_VOICE_ID:
            raise TtsError("missing voice id")
        url = (
            f"https://api.elevenlabs.io/v1/text-to-speech/"
            f"{settings.ELEVENLABS_VOICE_ID}?output_format=mp3_44100_128"
        )
        headers = {
            "xi-api-key": settings.ELEVENLABS_API_KEY.get_secret_value(),
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
        }
        payload = {
            "text": text,
            "model_id": settings.ELEVENLABS_MODEL,
            "language_code": "pt",
            "voice_settings": {
                "stability": 0.60,
                "similarity_boost": 0.75,
                "style": 0.15,
                "use_speaker_boost": True,
                "speed": 1.0,
            },
        }
        owned_client = client is None
        http = client or httpx.Client(timeout=settings.TTS_TIMEOUT_SECONDS)
        try:
            response = http.post(url, headers=headers, json=payload)
        except httpx.TransportError as exc:
            raise TtsError(f"transport error: {exc}") from exc
        finally:
            if owned_client:
                http.close()
        if response.status_code != 200:
            raise TtsError(f"status {response.status_code}")
        content = response.content
        if not content:
            raise TtsError("empty response")
        if len(content) > settings.TTS_RESPONSE_MAX_BYTES:
            raise TtsError("response too large")
        media_base64 = base64.b64encode(content).decode("ascii")
        return TtsResult(media_base64=media_base64, chars=len(text))


def make_tts_provider(provider_name: str) -> TTSProvider:
    """Factory do adapter — hoje só ``elevenlabs`` registrado."""
    providers = {"elevenlabs": ElevenLabsTTS}
    try:
        provider_cls = providers[provider_name]
    except KeyError:
        raise TtsError(f"unknown provider: {provider_name}")
    return provider_cls()


def tts_step(text: str, *, city: str | None = None) -> TtsResult | None:
    """Único caminho runtime de síntese; ``None`` = caller deve cair no fallback."""
    settings = get_settings()
    if settings.TTS_PROVIDER is None:
        return None
    normalized = normalize_for_tts(text, city=city)
    if not normalized.with_pauses:
        return None
    try:
        provider = make_tts_provider(settings.TTS_PROVIDER)
        return provider.synthesize(normalized.with_pauses, settings=settings)
    except TtsError:
        return None
