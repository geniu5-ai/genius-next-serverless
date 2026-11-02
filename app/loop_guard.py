"""Loop guard: deterministic bot-vs-bot circuit breaker.

Checked at webhook ingress, before any turn is created or LLM called
(app/main.py, right next to rate_limit_allow -- same pattern, same
engine-per-module style). Additive -- does not touch the reply pipeline,
the agent, the prompt, or existing tables.

Root cause (28-29/set/2026): a cold-outreach lead's own automated
WhatsApp responder (delivery bot, numeric-menu bot) exchanged messages
with our bot indefinitely -- 51 turns/16min in one case, 134+ inbound in
another, near-identical text every time. The prompt-level rule
(reply_normal v8, "2 respostas automaticas seguidas -> handoff") is a
soft signal the model does not reliably follow under a fast loop; this
is the hard backstop. Design consulted with Kimi K3 + GLM-5.3-flash
(OpenRouter, 28/set) -- both converged on the same shape: combine content
repetition with reply velocity, block only on BOTH signals together
(either alone has real false-positive risk against a fast or repetitive
but genuine human).
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import Engine, create_engine, make_url, text

from app.settings import Settings

_WINDOW = 5  # quantas mensagens recentes guardamos/avaliamos
_REPEAT_MIN_MATCHES = 3  # de _WINDOW, quantas iguais = "repeticao"
_FAST_INTER_ARRIVAL_SECONDS = 8.0  # bot tipico responde bem mais rapido
_BLOCK_HOURS = 24
# Teto absoluto (29/set/2026, achado em pesquisa externa -- ralph-claude-code
# e ralphy-looper usam o mesmo principio pra loop de auto-dev: alem da
# deteccao fina, sempre um circuit breaker numerico que dispara MESMO se a
# logica de repeticao/velocidade falhar por algum motivo -- ex.: bot do
# outro lado varia texto mais do que os 2 casos reais de 28/set variavam.
# "Estancia Parrilla" chegou a 134 inbound antes do guard fino existir.
_VOLUME_CEILING_MSGS = 15
_VOLUME_CEILING_MINUTES = 10
_HUMAN_ESCAPE_MIN_CHARS = 80  # mensagem longa = quase sempre humano digitando
_HUMAN_KEYWORDS = ("humano", "atendente", "pessoa", "gerente", "responsavel")

_DIGITS_RE = re.compile(r"\d+")
_SPACE_RE = re.compile(r"\s+")

_LOAD_SQL = text(
    "SELECT recent, status, unblock_at FROM genius_next.conversation_guard "
    "WHERE tenant_id = :tenant_id AND phone = :phone"
)
_VOLUME_COUNT_SQL = text(
    """
    SELECT count(*) FROM genius_next.events
    WHERE tenant_id = :tenant_id AND phone = :phone AND direction = 'inbound'
      AND received_at > now() - make_interval(mins => :minutes)
    """
)
_UPSERT_OK_SQL = text(
    """
    INSERT INTO genius_next.conversation_guard (tenant_id, phone, recent, status, updated_at)
    VALUES (:tenant_id, :phone, :recent, 'ok', now())
    ON CONFLICT (tenant_id, phone) DO UPDATE SET
        recent = EXCLUDED.recent, status = 'ok',
        blocked_at = NULL, unblock_at = NULL, updated_at = now()
    """
)
_UPSERT_BLOCK_SQL = text(
    """
    INSERT INTO genius_next.conversation_guard
        (tenant_id, phone, recent, status, blocked_at, unblock_at, updated_at)
    VALUES (:tenant_id, :phone, :recent, 'blocked', now(), now() + make_interval(hours => :hours), now())
    ON CONFLICT (tenant_id, phone) DO UPDATE SET
        recent = EXCLUDED.recent, status = 'blocked',
        blocked_at = now(), unblock_at = now() + make_interval(hours => :hours), updated_at = now()
    """
)


@lru_cache(maxsize=1)
def get_loop_guard_engine(database_url: str) -> Engine:
    """Build the shared engine lazily; creating it does not open a connection."""
    url = make_url(database_url)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    return create_engine(url, pool_pre_ping=True)


def _normalize(msg_text: str) -> str:
    """lowercase, sem acento, digitos viram '#' (pega 'as 18h'/'as 19h' como
    o mesmo padrao), espaco colapsado. Mensagens de bot variam placeholder,
    nao a estrutura -- normalizar isso e' o que faz a comparacao pegar."""
    decomposed = unicodedata.normalize("NFKD", msg_text or "")
    sem_acento = "".join(c for c in decomposed if not unicodedata.combining(c))
    sem_digito = _DIGITS_RE.sub("#", sem_acento.lower())
    return _SPACE_RE.sub(" ", sem_digito).strip()


def _is_human_escape(normalized: str, raw: str) -> bool:
    if len(raw) >= _HUMAN_ESCAPE_MIN_CHARS:
        return True
    return any(kw in normalized for kw in _HUMAN_KEYWORDS)


@dataclass(frozen=True)
class GuardDecision:
    should_process: bool
    reason: str = ""


def loop_guard_allow(*, tenant_id: str, phone: str, msg_text: str, settings: Settings) -> GuardDecision:
    """Chamar no webhook, ANTES de criar turno. should_process=False =
    retorna 200 sem enfileirar nada, nunca erro, nunca reply -- mesma
    filosofia do rate_limit_allow (falha/bloqueio e' sempre silencioso pro
    remetente, nunca vaza detalhe de guarda de volta)."""
    engine = get_loop_guard_engine(settings.DATABASE_URL)
    now = time.time()
    normalized = _normalize(msg_text)

    with engine.begin() as conn:
        row = conn.execute(_LOAD_SQL, {"tenant_id": tenant_id, "phone": phone}).mappings().first()

        if row is not None and row["status"] == "blocked":
            unblock_at = row["unblock_at"]
            if unblock_at is not None and unblock_at.timestamp() > now:
                return GuardDecision(should_process=False, reason="loop_blocked")
            # cooldown expirado -- reabre pra tentar de novo (pode ter virado humano)

        # Teto absoluto: roda ANTES de qualquer outra logica (inclusive do
        # escape de humano) -- e' o ultimo recurso, tem que disparar mesmo
        # que a deteccao fina falhe por completo.
        volume = conn.execute(
            _VOLUME_COUNT_SQL,
            {"tenant_id": tenant_id, "phone": phone, "minutes": _VOLUME_CEILING_MINUTES},
        ).scalar_one()
        if volume >= _VOLUME_CEILING_MSGS:
            conn.execute(
                _UPSERT_BLOCK_SQL,
                {"tenant_id": tenant_id, "phone": phone, "recent": "[]", "hours": _BLOCK_HOURS},
            )
            return GuardDecision(should_process=False, reason="loop_volume_ceiling")

        recent: list[dict] = list(row["recent"]) if row is not None else []

        if _is_human_escape(normalized, msg_text or ""):
            # mensagem longa/com palavra humana: nunca conta como bot, e
            # reseta o historico -- evita que respostas curtas de bot
            # antes de 1 mensagem real de humano ainda pareçam loop depois.
            conn.execute(_UPSERT_OK_SQL, {"tenant_id": tenant_id, "phone": phone, "recent": "[]"})
            return GuardDecision(should_process=True)

        recent.append({"text": normalized, "ts": now})
        recent = recent[-_WINDOW:]

        should_block = False
        if len(recent) >= _REPEAT_MIN_MATCHES:
            textos = [r["text"] for r in recent]
            repeticoes = max(textos.count(t) for t in set(textos))
            intervalos = [b["ts"] - a["ts"] for a, b in zip(recent, recent[1:])]
            rapido = bool(intervalos) and (sum(intervalos) / len(intervalos)) < _FAST_INTER_ARRIVAL_SECONDS
            if repeticoes >= _REPEAT_MIN_MATCHES and rapido:
                should_block = True

        if should_block:
            conn.execute(
                _UPSERT_BLOCK_SQL,
                {
                    "tenant_id": tenant_id,
                    "phone": phone,
                    "recent": json.dumps(recent),
                    "hours": _BLOCK_HOURS,
                },
            )
            return GuardDecision(should_process=False, reason="loop_detected")

        conn.execute(_UPSERT_OK_SQL, {"tenant_id": tenant_id, "phone": phone, "recent": json.dumps(recent)})
        return GuardDecision(should_process=True)
