"""Rota de midia durável: baixar, extrair e gravar o texto rotulado da midia.

Disparo: a ingestao enfileira ``media_extract_job`` logo depois de persistir a
midia (dentro da janela de debounce, e tambem quando a conversa esta com a
equipe). O turno chama a MESMA ``extract_media_once``: uma trava do Postgres por
midia garante uma extracao so, e quem chega depois le o que ja foi gravado.

Privacidade: os bytes ficam so em memoria, dentro de UMA extracao.
``genius_next.event_media`` guarda so metadados (sem texto, sem URL, sem bytes).
O texto extraido mora em ``events.text_content``, como qualquer mensagem digitada.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.contracts import MEDIA_KINDS, EvolutionWebhook, media_kind_of
from app.db import get_engine, sql_session, transaction
from app.identity_primitives import stable_id
from app.media.extraction import (
    FORMAT_REASONS,
    MediaOutcome,
    compose_media_text,
    extract_from_bytes,
    failed_outcome,
    label_for,
)
from app.media.fetch import MediaFailure, MediaJob, MediaTransientError, fetch_media
from app.jobs import AUX_QUEUE, defer_once, jobs
from app.settings import get_settings

logger = logging.getLogger(__name__)

# Textos fixos, sem LLM e sem eco do que o cliente escreveu.
AUDIO_FALLBACK_TEXT = (
    "Vi que você me mandou um áudio, mas não consegui ouvir direito agora — "
    "pode me escrever o que precisa?"
)
MEDIA_FALLBACK_TEXT = (
    "Recebi seu arquivo, mas não consegui abrir aqui. Pode me escrever o que "
    "precisa ou mandar de novo?"
)
MEDIA_FORMAT_FALLBACK_TEXT = (
    "Recebi seu arquivo, mas esse formato não abre por aqui. Consegue mandar uma "
    "foto ou um PDF? Se preferir, me escreve o que precisa."
)
MEDIA_HANDOFF_TEXT = (
    "Não consegui abrir seu arquivo por aqui. Vou passar para a nossa equipe, "
    "que continua com você."
)


# --------------------------------------------------------------------------- #
# Escopo e job
# --------------------------------------------------------------------------- #


def media_route_enabled(tenant_id: str, settings: Any | None = None) -> bool:
    """Mesmo parser de SAFETY_ROUTE_TENANTS; ``*`` vale para todos."""
    resolved = settings if settings is not None else get_settings()
    raw = getattr(resolved, "MEDIA_ROUTE_TENANTS", "") or ""
    tenants = {item.strip() for item in raw.split(",") if item.strip()}
    return "*" in tenants or tenant_id in tenants


def build_media_job(
    *,
    tenant_id: str,
    provider: str,
    instance: str,
    message_id: str,
    payload: EvolutionWebhook,
) -> MediaJob | None:
    """Job deterministico a partir do payload saneado (mesmo no ingest e no turno)."""
    kind = media_kind_of(payload, provider)
    if kind not in MEDIA_KINDS or provider not in ("zernio", "evolution"):
        return None
    data = payload.data
    body = data.message
    ref = data.genius_media if provider == "zernio" else None
    if ref is not None:
        return MediaJob(
            tenant_id=tenant_id,
            provider=provider,  # type: ignore[arg-type]
            instance=instance,
            message_id=message_id,
            kind=ref.kind,
            mimetype=ref.mimetype,
            media_id=ref.media_id,
            sha256=ref.sha256,
            voice=bool(ref.voice),
            file_name=ref.file_name,
        )
    mimetype: str | None = None
    sha256: str | None = None
    file_name: str | None = None
    voice = False
    if kind == "image" and body.image_message is not None:
        mimetype = body.image_message.mimetype
    elif kind == "video" and body.video_message is not None:
        mimetype = body.video_message.mimetype
    elif kind == "audio" and body.audio_message is not None:
        mimetype = body.audio_message.mimetype
        sha256 = body.audio_message.file_sha256
        voice = bool(body.audio_message.ptt)
    elif kind == "document" and body.document is not None:
        mimetype = body.document.mimetype
        file_name = body.document.file_name
    return MediaJob(
        tenant_id=tenant_id,
        provider=provider,  # type: ignore[arg-type]
        instance=instance,
        message_id=message_id,
        kind=kind,  # type: ignore[arg-type]
        mimetype=(mimetype or None) and mimetype[:128],
        media_id=None,
        sha256=(sha256 or None) and sha256[:128],
        voice=voice,
        file_name=(file_name or None) and file_name[:256],
    )


def evolution_media_available(payload: EvolutionWebhook, settings: Any) -> bool:
    """Evolution so entra na rota com busca ligada ou base64 no proprio evento."""
    return bool(getattr(settings, "MEDIA_EVOLUTION_FETCH_ENABLED", False)) or (
        payload.data.message.inline_base64 is not None
    )


# --------------------------------------------------------------------------- #
# Persistencia
# --------------------------------------------------------------------------- #

_EVENT_MEDIA_INSERT_SQL = text(
    """
    /* event_media.insert */
    INSERT INTO genius_next.event_media (
        tenant_id, provider, message_id, kind, mimetype, size_bytes, status,
        reason, extractor, model, prompt_tokens, completion_tokens, cost_usd
    ) VALUES (
        :tenant_id, :provider, :message_id, :kind, :mimetype, :size_bytes, :status,
        :reason, :extractor, :model, :prompt_tokens, :completion_tokens, :cost_usd
    )
    ON CONFLICT (tenant_id, provider, message_id) DO NOTHING
    RETURNING id
    """
)

_EVENT_FOR_MEDIA_SQL = text(
    """
    /* event_media.event_lock */
    SELECT id, text_content
    FROM genius_next.events
    WHERE tenant_id = :tenant_id
      AND provider = :provider
      AND message_id = :message_id
      AND direction = 'inbound'
    FOR UPDATE
    """
)

_EVENT_MEDIA_UPDATE_SQL = text(
    """
    /* event_media.event_update */
    UPDATE genius_next.events
    SET text_content = :text_content,
        media_url = :media_url
    WHERE id = :event_id
      AND tenant_id = :tenant_id
    """
)

_EVENT_MEDIA_UPDATE_STRIP_SQL = text(
    """
    /* event_media.event_update_strip */
    UPDATE genius_next.events
    SET text_content = :text_content,
        media_url = :media_url,
        raw_payload = raw_payload
            #- '{data,message,base64}'
            #- '{data,message,audioMessage,base64}'
    WHERE id = :event_id
      AND tenant_id = :tenant_id
    """
)

_MEDIA_QUOTA_SQL = text(
    """
    /* event_media.quota */
    SELECT count(*)
    FROM genius_next.event_media
    WHERE tenant_id = :tenant_id
      AND created_at >= (
          date_trunc('day', now() AT TIME ZONE 'America/Sao_Paulo')
          AT TIME ZONE 'America/Sao_Paulo'
      )
    """
)

_MEDIA_FAILURES_RECENT_SQL = text(
    """
    /* event_media.failures_recent */
    SELECT count(*)
    FROM genius_next.event_media AS m
    JOIN genius_next.events AS e
      ON e.tenant_id = m.tenant_id
     AND e.provider = m.provider
     AND e.message_id = m.message_id
    WHERE m.tenant_id = :tenant_id
      AND m.provider = :provider
      AND m.status = 'failed'
      AND m.created_at > now() - interval '24 hours'
      AND e.phone = :phone
      AND m.message_id NOT IN :exclude_message_ids
    """
).bindparams(bindparam("exclude_message_ids", expanding=True))


def record_media_result_with_session(
    session: Session, job: MediaJob, outcome: MediaOutcome
) -> bool:
    """Grava o resultado uma vez so; devolve False quando ja estava gravado."""
    inserted = session.execute(
        _EVENT_MEDIA_INSERT_SQL,
        {
            "tenant_id": job.tenant_id,
            "provider": job.provider,
            "message_id": job.message_id,
            "kind": job.kind,
            "mimetype": outcome.mimetype or job.mimetype,
            "size_bytes": outcome.size_bytes,
            "status": outcome.status,
            "reason": outcome.reason,
            "extractor": outcome.extractor,
            "model": outcome.model,
            "prompt_tokens": outcome.prompt_tokens,
            "completion_tokens": outcome.completion_tokens,
            "cost_usd": outcome.cost_usd,
        },
    ).scalar_one_or_none()
    if inserted is None:
        return False
    row = (
        session.execute(
            _EVENT_FOR_MEDIA_SQL,
            {
                "tenant_id": job.tenant_id,
                "provider": job.provider,
                "message_id": job.message_id,
            },
        )
        .mappings()
        .first()
    )
    if row is None:
        return True
    params = {
        "event_id": row["id"],
        "tenant_id": job.tenant_id,
        "text_content": compose_media_text(row["text_content"], outcome),
        "media_url": f"{job.provider}:{job.media_id or job.message_id}",
    }
    sql = _EVENT_MEDIA_UPDATE_STRIP_SQL if job.provider == "evolution" else _EVENT_MEDIA_UPDATE_SQL
    session.execute(sql, params)
    return True


@transaction()
def record_media_result_txn(job: MediaJob, outcome: MediaOutcome) -> bool:
    return record_media_result_with_session(sql_session(), job, outcome)


def media_quota_ok_with_session(session: Session, tenant_id: str, limit: int) -> bool:
    used = session.execute(_MEDIA_QUOTA_SQL, {"tenant_id": tenant_id}).scalar_one()
    return int(used or 0) < limit


@transaction()
def media_quota_ok_txn(tenant_id: str, limit: int) -> bool:
    return media_quota_ok_with_session(sql_session(), tenant_id, limit)


def media_failures_recent_with_session(
    session: Session,
    tenant_id: str,
    provider: str,
    phone: str,
    exclude_message_ids: list[str],
) -> int:
    # NOT IN com lista vazia nao e SQL valido: um id impossivel ocupa o lugar.
    excluded = list(exclude_message_ids) or ["\x01none"]
    count = session.execute(
        _MEDIA_FAILURES_RECENT_SQL,
        {
            "tenant_id": tenant_id,
            "provider": provider,
            "phone": phone,
            "exclude_message_ids": excluded,
        },
    ).scalar_one()
    return int(count or 0)


@transaction()
def media_failures_recent_txn(
    tenant_id: str, provider: str, phone: str, exclude_message_ids: list[str]
) -> int:
    return media_failures_recent_with_session(
        sql_session(), tenant_id, provider, phone, exclude_message_ids
    )


# --------------------------------------------------------------------------- #
# Workflow
# --------------------------------------------------------------------------- #


def run_media_extraction(job: MediaJob, settings: Any, client: Any = None) -> MediaOutcome:
    """Fetch + extracao. Levanta so ``MediaTransientError``."""
    fetched = fetch_media(job, settings, client)
    if isinstance(fetched, MediaFailure):
        return failed_outcome(
            job.kind,
            fetched.reason,
            mimetype=fetched.mimetype or job.mimetype,
            size_bytes=fetched.size_bytes,
        )
    return extract_from_bytes(job.kind, fetched.mimetype, fetched.data, settings, client)


def extract_media_step(job: MediaJob) -> MediaOutcome:
    """Baixa e extrai de uma vez (os bytes nunca saem da memoria); 2 tentativas se transitorio."""
    settings = get_settings()
    for attempt in (1, 2):
        try:
            return run_media_extraction(job, settings)
        except MediaTransientError:
            if attempt == 2:
                raise
            time.sleep(3)
    raise AssertionError("inalcancavel")


def _log_outcome(job: MediaJob, outcome: MediaOutcome) -> None:
    logger.info(
        "media extracted: tenant=%s kind=%s status=%s reason=%s bytes=%s ms=%s model=%s tokens=%s/%s",
        job.tenant_id,
        job.kind,
        outcome.status,
        outcome.reason,
        outcome.size_bytes,
        outcome.elapsed_ms,
        outcome.model,
        outcome.prompt_tokens,
        outcome.completion_tokens,
    )


_EVENT_MEDIA_ROW_SQL = text(
    """
    /* event_media.row */
    SELECT m.kind, m.mimetype, m.size_bytes, m.status, m.reason, m.extractor, m.model,
           (SELECT e.text_content FROM genius_next.events AS e
             WHERE e.tenant_id = m.tenant_id AND e.provider = m.provider
               AND e.message_id = m.message_id AND e.direction = 'inbound' LIMIT 1) AS composed
    FROM genius_next.event_media AS m
    WHERE m.tenant_id = :tenant_id AND m.provider = :provider AND m.message_id = :message_id
    """
)
_MEDIA_LOCK_POLL_S = 0.25


def _stored_outcome(job: MediaJob) -> tuple[MediaOutcome, str] | None:
    """Resultado ja gravado: metadados de ``event_media`` + o texto composto do evento."""
    engine = get_engine(get_settings().DATABASE_URL)
    with Session(engine) as session:
        row = (
            session.execute(
                _EVENT_MEDIA_ROW_SQL,
                {"tenant_id": job.tenant_id, "provider": job.provider, "message_id": job.message_id},
            )
            .mappings()
            .first()
        )
    if row is None:
        return None
    outcome = MediaOutcome(
        status=row["status"],
        kind=row["kind"],
        label=label_for(row["kind"], row["mimetype"]),
        reason=row["reason"],
        size_bytes=row["size_bytes"],
        mimetype=row["mimetype"],
        extractor=row["extractor"],
        model=row["model"],
    )
    return outcome, row["composed"] or ""


def extract_media_once(job: MediaJob, *, timeout: float) -> tuple[MediaOutcome, str | None]:
    """Uma extracao por midia. Devolve ``(resultado, texto composto ja gravado | None)``.

    A trava do Postgres (por midia) faz o job de chegada e o turno nunca extrairem a mesma midia
    duas vezes: o segundo espera ate ``timeout`` e le o resultado gravado pelo primeiro. Nunca
    levanta: toda falha vira ``MediaOutcome`` failed com motivo.
    """
    settings = get_settings()
    engine = get_engine(settings.DATABASE_URL)
    key = stable_id("media", job.tenant_id, job.provider, job.message_id)
    deadline = time.monotonic() + timeout
    with engine.connect() as conn:
        while not conn.execute(
            text("SELECT pg_try_advisory_lock(hashtextextended(:k, 0))"), {"k": key}
        ).scalar_one():
            if time.monotonic() >= deadline:
                return failed_outcome(job.kind, "timeout", mimetype=job.mimetype), None
            time.sleep(_MEDIA_LOCK_POLL_S)
        try:
            stored = _stored_outcome(job)
            if stored is not None:
                return stored[0], stored[1]
            if not media_quota_ok_txn(job.tenant_id, settings.MEDIA_DAILY_MAX_PER_TENANT):
                outcome = failed_outcome(job.kind, "local_quota", mimetype=job.mimetype)
            else:
                try:
                    outcome = extract_media_step(job)
                except Exception:
                    logger.warning(
                        "media extraction exhausted: tenant=%s kind=%s", job.tenant_id, job.kind
                    )
                    outcome = failed_outcome(
                        job.kind, "transient_exhausted", mimetype=job.mimetype
                    )
            try:
                record_media_result_txn(job, outcome)
            except Exception:
                logger.warning(
                    "media result not recorded: tenant=%s kind=%s", job.tenant_id, job.kind,
                    exc_info=True,
                )
            _log_outcome(job, outcome)
            return outcome, None
        finally:
            conn.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:k, 0))"), {"k": key}
            )


@jobs.task(name="media_extract", queue=AUX_QUEUE)
def media_extract_job(job: dict[str, Any]) -> None:
    extract_media_once(MediaJob.model_validate(job), timeout=float(get_settings().MEDIA_TURN_WAIT_SECONDS))


def enqueue_media_extraction(job: MediaJob) -> None:
    """Agenda a extracao; a mesma midia nao entra duas vezes na fila."""
    defer_once(
        media_extract_job,
        queueing_lock=stable_id("media-job", job.tenant_id, job.provider, job.message_id),
        job=job.model_dump(mode="json"),
    )


def maybe_enqueue_media_on_ingest(event: Any) -> None:
    """Disparo best-effort no ingest (o turno reinicia pelo mesmo ID se falhar).

    Nunca levanta: uma falha aqui nao pode derrubar a ingestao do evento.
    """
    try:
        settings = get_settings()
        if not media_route_enabled(event.tenant_id, settings):
            return
        provider = event.identity.provider
        payload = event.sanitized_payload
        if provider == "evolution" and not evolution_media_available(payload, settings):
            return
        job = build_media_job(
            tenant_id=event.tenant_id,
            provider=provider,
            instance=event.identity.instance,
            message_id=event.identity.message_id,
            payload=payload,
        )
        if job is None:
            return
        enqueue_media_extraction(job)
    except Exception:
        logger.warning(
            "media enqueue failed on ingest: tenant=%s", event.tenant_id, exc_info=True
        )


# --------------------------------------------------------------------------- #
# Turno
# --------------------------------------------------------------------------- #


class TurnMedia(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    turn: Any
    outcome: Literal["proceed", "fallback", "handoff", "silent"]
    fallback_text: str | None = None
    safety_texts: list[str] = Field(default_factory=list)
    voice_ok: bool = True


def _await_outcome(
    job: MediaJob, settings: Any, *, start: bool
) -> tuple[MediaOutcome, str | None]:
    if not start:
        # Evolution sem busca e sem base64 no claim: o base64 pode ja ter sido
        # removido por uma extracao concluida. So confere, sem extrair nada.
        stored = _stored_outcome(job)
        if stored is None:
            return failed_outcome(job.kind, "disabled", mimetype=job.mimetype), None
        return stored
    return extract_media_once(job, timeout=float(settings.MEDIA_TURN_WAIT_SECONDS))


def enrich_turn_media(turn: Any, *, phone: str | None = None) -> TurnMedia:
    """Rotula a midia do turno e decide entre seguir, fallback, handoff ou silencio."""
    settings = get_settings()
    tenant_id = turn.tenant_id
    provider = getattr(turn, "provider", "evolution")
    enabled = media_route_enabled(tenant_id, settings)
    events: list[Any] = []
    safety_texts: list[str] = []
    usable = False
    failures: list[MediaOutcome] = []
    current_message_ids: list[str] = []
    changed = False

    for event in getattr(turn, "events", None) or []:
        if getattr(event, "direction", None) != "inbound":
            events.append(event)
            continue
        payload = getattr(event, "sanitized_payload", None)
        kind = media_kind_of(payload, provider) if payload is not None else None
        if kind is None:
            text_value = (getattr(event, "text_content", None) or "").strip()
            if text_value:
                usable = True
                safety_texts.append(text_value)
            events.append(event)
            continue
        if kind == "silent":
            events.append(event)
            continue
        current_message_ids.append(event.message_id)
        caption = (payload.text_content or "").strip() or None
        job = build_media_job(
            tenant_id=tenant_id,
            provider=provider,
            instance=turn.instance,
            message_id=event.message_id,
            payload=payload,
        )
        stored_text: str | None = None
        if job is None or not enabled:
            outcome = failed_outcome(kind, "disabled")
        elif provider == "evolution" and not evolution_media_available(payload, settings):
            outcome, stored_text = _await_outcome(job, settings, start=False)
        else:
            outcome, stored_text = _await_outcome(job, settings, start=True)
        new_text = stored_text if stored_text else compose_media_text(caption, outcome)
        if new_text != event.text_content:
            changed = True
        events.append(event.model_copy(update={"text_content": new_text}))
        if caption:
            usable = True
            safety_texts.append(caption)
        if outcome.status == "ok":
            usable = True
            audio_text = outcome.text or (stored_text if outcome.kind == "audio" else None)
            if outcome.kind == "audio" and audio_text:
                safety_texts.append(audio_text)
        else:
            failures.append(outcome)

    enriched = turn.model_copy(update={"events": events}) if changed else turn

    if not usable and not failures:
        return TurnMedia(turn=enriched, outcome="silent", voice_ok=False)
    if usable:
        return TurnMedia(turn=enriched, outcome="proceed", safety_texts=safety_texts)

    # Nenhum texto utilizavel e ao menos uma midia falhou.
    if enabled and phone:
        try:
            recent = media_failures_recent_txn(
                tenant_id, provider, phone, current_message_ids
            )
        except Exception:
            logger.warning("media failure history unavailable for tenant=%s", tenant_id)
            recent = 0
        if recent >= 1:
            return TurnMedia(
                turn=enriched,
                outcome="handoff",
                fallback_text=MEDIA_HANDOFF_TEXT,
                safety_texts=safety_texts,
                voice_ok=False,
            )
    if all(failure.kind == "audio" for failure in failures):
        text_value = AUDIO_FALLBACK_TEXT
    elif any(failure.reason in FORMAT_REASONS for failure in failures):
        text_value = MEDIA_FORMAT_FALLBACK_TEXT
    else:
        text_value = MEDIA_FALLBACK_TEXT
    return TurnMedia(
        turn=enriched,
        outcome="fallback",
        fallback_text=text_value,
        safety_texts=safety_texts,
        voice_ok=False,
    )
