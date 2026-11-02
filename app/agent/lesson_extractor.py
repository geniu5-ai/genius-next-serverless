"""agent lesson extractor — automatic extraction of tactical lessons.

Batch CLI that scans ``genius_next.conversations`` never extracted before
(``lessons_extracted_at IS NULL``) with at least ``_MIN_EVENTS`` events holding
non-null ``text_content``, rebuilds a Cliente/Assistente transcript ordered by
``conversation_sequence``, asks OpenRouter (nvidia/nemotron-3-ultra-550b-a55b:free)
for 0-5 tactical lessons as a JSON array, and persists each lesson via
``app.agent.lessons.save_lesson`` (which also embeds via Jina). Marks the
conversation as extracted afterwards.

NOTE (deviation from a naive reading of lessons.source_conversation_id):
``genius_next.conversations`` has a composite PK and NO ``id`` column, so this
module passes ``source_conversation_id=None`` to save_lesson (see
migrations/004_lessons.sql — the column is a conceptual bigint reference only).

Runnable as:  uv run python -m app.agent.lesson_extractor [--dry-run] [--tenant T]
"""

from __future__ import annotations

import argparse
import json
import logging
import os

import httpx
from sqlalchemy import create_engine, make_url, text as sql_text
from sqlalchemy.orm import Session, sessionmaker

from app.agent.lessons import infer_segmento, save_lesson

logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_OPENROUTER_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
_OPENROUTER_TEMPERATURE = 0.3
_OPENROUTER_TIMEOUT_SECONDS = 120.0
_MIN_EVENTS = 4
_BATCH_SIZE = 20
_USER_AGENT = "genius-next-lesson-extractor/1.0"

_SYSTEM_PROMPT = (
    "Analise o diálogo comercial. Extraia 0-5 lições táticas acionáveis. "
    'Responda APENAS JSON array: [{"content":"...","confidence":0.0-1.0}]. '
    "Sem lições úteis = []."
)

# ── SQL ─────────────────────────────────────────────────────────────────────

_DDL_ADD_LESSONS_EXTRACTED_AT = sql_text(
    """/* agent.lesson_extractor.ddl */
    ALTER TABLE genius_next.conversations
        ADD COLUMN IF NOT EXISTS lessons_extracted_at timestamptz
    """
)

_CANDIDATE_CONVERSATIONS_SQL = sql_text(
    """/* agent.lesson_extractor.candidates */
    SELECT c.tenant_id, c.provider, c.instance, c.remote_jid,
           COUNT(e.id) AS event_count
    FROM genius_next.conversations AS c
    JOIN genius_next.events AS e
      ON e.tenant_id = c.tenant_id
     AND e.provider = c.provider
     AND e.instance = c.instance
     AND e.remote_jid = c.remote_jid
     AND e.text_content IS NOT NULL
    WHERE c.lessons_extracted_at IS NULL
      AND (CAST(:tenant_id AS text) IS NULL OR c.tenant_id = :tenant_id)
    GROUP BY c.tenant_id, c.provider, c.instance, c.remote_jid
    HAVING COUNT(e.id) >= :min_events
    ORDER BY c.updated_at
    LIMIT :limit
    """
)

_CONVERSATION_EVENTS_SQL = sql_text(
    """/* agent.lesson_extractor.events */
    SELECT direction, text_content
    FROM genius_next.events
    WHERE tenant_id = :tenant_id
      AND provider = :provider
      AND instance = :instance
      AND remote_jid = :remote_jid
      AND text_content IS NOT NULL
    ORDER BY conversation_sequence
    """
)

_MARK_EXTRACTED_SQL = sql_text(
    """/* agent.lesson_extractor.mark */
    UPDATE genius_next.conversations
    SET lessons_extracted_at = now(),
        updated_at = now()
    WHERE tenant_id = :tenant_id
      AND provider = :provider
      AND instance = :instance
      AND remote_jid = :remote_jid
    """
)


# ── Schema / seleção ────────────────────────────────────────────────────────


def ensure_schema(engine) -> None:
    """Add genius_next.conversations.lessons_extracted_at if missing (idempotent).

    Runs the ALTER TABLE ... ADD COLUMN IF NOT EXISTS so the candidates query
    never fails on fresh databases. Safe to call on every run.
    """
    with engine.begin() as conn:
        conn.execute(_DDL_ADD_LESSONS_EXTRACTED_AT)


def fetch_candidate_conversations(
    session: Session,
    *,
    tenant_id: str | None = None,
    limit: int = _BATCH_SIZE,
) -> list[dict]:
    """Return conversations never extracted with >= _MIN_EVENTS non-null text events.

    One dict per row: {"tenant_id", "provider", "instance", "remote_jid",
    "event_count"}. Ordered by oldest updated_at first, capped at `limit`.
    `tenant_id` filters to one tenant when provided.
    """
    result = session.execute(
        _CANDIDATE_CONVERSATIONS_SQL,
        {
            "tenant_id": tenant_id,
            "min_events": _MIN_EVENTS,
            "limit": limit,
        },
    )
    rows = result.mappings().all()
    return [dict(r) for r in rows]


# ── Extração ────────────────────────────────────────────────────────────────


def build_transcript(rows: list[dict]) -> str:
    """Format events into a "Cliente:/Assistente:" transcript for the LLM.

    Input rows are mappings with "direction" and "text_content" already ordered
    by conversation_sequence. inbound -> "Cliente: <text>", outbound ->
    "Assistente: <text>", unknown directions are skipped. Joined with "\\n".

    Example:
        [{"direction": "inbound", "text_content": "Oi"},
         {"direction": "outbound", "text_content": "Olá!"}]
        -> "Cliente: Oi\\nAssistente: Olá!"
    """
    lines = []
    for row in rows:
        direction = row.get("direction")
        text = row.get("text_content")
        if direction == "inbound":
            lines.append(f"Cliente: {text}")
        elif direction == "outbound":
            lines.append(f"Assistente: {text}")
        # else skip
    return "\n".join(lines)


def _parse_lessons_json(content: str) -> list[dict]:
    """Parse the model's JSON-array reply into [{content, confidence}].

    Tolerates ```json fences by slicing between the first "[" and the last "]".
    Each item: content must be a non-empty str (stripped); confidence is a float
    clamped to 0.0-1.0 when present, None when absent. Raises ValueError on
    invalid structure instead of returning partial data.
    """
    start = content.find('[')
    end = content.rfind(']')
    if start == -1 or end == -1 or end < start:
        raise ValueError("No JSON array found in model reply")
    json_str = content[start:end+1]
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {e}") from e
    if not isinstance(data, list):
        raise ValueError("Expected JSON array")
    lessons = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("Each lesson must be an object")
        content_val = item.get("content")
        if not isinstance(content_val, str) or not content_val.strip():
            raise ValueError("Lesson content must be a non-empty string")
        content_val = content_val.strip()
        conf = item.get("confidence")
        if conf is not None:
            if not isinstance(conf, (int, float)):
                raise ValueError("confidence must be a number")
            conf = float(conf)
            conf = max(0.0, min(1.0, conf))
        lessons.append({"content": content_val, "confidence": conf})
    return lessons


def extract_lessons(transcript: str) -> list[dict]:
    """Call OpenRouter and return parsed lessons [{content, confidence}].

    Requires env OPENROUTER_API_KEY (raises RuntimeError with a clear message
    when missing). POSTs to _OPENROUTER_URL with model/temperature from config,
    headers Authorization Bearer + User-Agent, body system prompt + transcript.
    Non-200 responses and invalid replies raise.
    """
    # 19/out/2026: LESSON_LLM_URL / LESSON_LLM_API_KEY / LESSON_LLM_MODEL trocam a
    # rota sem tocar no codigo (a chave do OpenRouter esgotou). Sem elas, o
    # comportamento e o de sempre: OpenRouter com OPENROUTER_API_KEY.
    api_key = os.environ.get("LESSON_LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY environment variable is not set")
    llm_url = os.environ.get("LESSON_LLM_URL") or _OPENROUTER_URL
    llm_model = os.environ.get("LESSON_LLM_MODEL") or _OPENROUTER_MODEL
    with httpx.Client(timeout=_OPENROUTER_TIMEOUT_SECONDS) as client:
        response = client.post(
            llm_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "User-Agent": _USER_AGENT,
            },
            json={
                "model": llm_model,
                "temperature": _OPENROUTER_TEMPERATURE,
                # Sem stream:false o Omni liga stream_options e a OpenAI responde 400 (TRAPS #70).
                "stream": False,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": transcript},
                ],
            },
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        return _parse_lessons_json(content)


def process_conversation(
    session: Session,
    *,
    tenant_id: str,
    provider: str,
    instance: str,
    remote_jid: str,
    dry_run: bool,
) -> list[dict]:
    """Extract lessons for one conversation; save + mark unless dry_run.

    Loads the conversation's events (ordered by conversation_sequence), builds
    the transcript, calls extract_lessons, then for each lesson calls
    save_lesson(session, tenant_id=..., segmento=<inferred>, content=...,
    source_conversation_id=None, confidence=...) and finally marks
    lessons_extracted_at=now() via _MARK_EXTRACTED_SQL. Commits once at the end
    (skipped in dry_run). Returns the extracted lessons list. Raises on
    API/parse/save failure (caller reports and continues the batch).
    """
    result = session.execute(
        _CONVERSATION_EVENTS_SQL,
        {
            "tenant_id": tenant_id,
            "provider": provider,
            "instance": instance,
            "remote_jid": remote_jid,
        },
    )
    rows = result.mappings().all()
    transcript = build_transcript(rows)
    if not transcript:
        return []
    lessons = extract_lessons(transcript)
    segmento = infer_segmento(transcript)
    if not dry_run:
        for lesson in lessons:
            save_lesson(
                session,
                tenant_id=tenant_id,
                segmento=segmento,
                content=lesson["content"],
                source_conversation_id=None,
                confidence=lesson.get("confidence"),
            )
        session.execute(
            _MARK_EXTRACTED_SQL,
            {
                "tenant_id": tenant_id,
                "provider": provider,
                "instance": instance,
                "remote_jid": remote_jid,
            },
        )
        session.commit()
    return lessons


# ── CLI ─────────────────────────────────────────────────────────────────────


def run(*, dry_run: bool = False, tenant_id: str | None = None) -> None:
    """Batch entrypoint: ensure schema, scan, process, report to stdout.

    Creates the engine from env DATABASE_URL (raises RuntimeError with a clear
    message when missing), a sessionmaker(bind=engine), ensures the schema, and
    processes up to _BATCH_SIZE candidate conversations. Prints per-conversation
    lessons and a final summary with counts (processadas / falhas / lessons).
    """
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL environment variable is not set")
    url = make_url(database_url)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    engine = create_engine(url, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine)
    ensure_schema(engine)
    with SessionLocal() as session:
        candidates = fetch_candidate_conversations(session, tenant_id=tenant_id)
        processed = 0
        failed = 0
        total_lessons = 0
        for cand in candidates:
            try:
                lessons = process_conversation(
                    session,
                    tenant_id=cand["tenant_id"],
                    provider=cand["provider"],
                    instance=cand["instance"],
                    remote_jid=cand["remote_jid"],
                    dry_run=dry_run,
                )
            except Exception as e:
                logger.warning("Failed to process conversation %s: %s", cand, e)
                failed += 1
                continue
            processed += 1
            total_lessons += len(lessons)
            print(
                f"Conversa {cand['tenant_id']}/{cand['provider']}/"
                f"{cand['instance']}/{cand['remote_jid']} "
                f"(event_count={cand['event_count']})"
            )
            for lesson in lessons:
                print(f"  - {lesson['content']} (confidence={lesson['confidence']})")
        mode = "DRY-RUN" if dry_run else "PROCESSADO"
        print(f"Resumo ({mode}): processadas={processed}, falhas={failed}, lições={total_lessons}")
        if failed and not processed:
            # 20/out/2026 (TRAPS #70): lote inteiro falhando era so WARNING e ninguem via. Agora e ERROR.
            logger.error("lesson_extractor: lote inteiro falhou (falhas=%d, processadas=0)", failed)


def main(argv: list[str] | None = None) -> None:
    """CLI: `--dry-run` shows without saving, `--tenant T` filters by tenant."""
    parser = argparse.ArgumentParser(description="agent lesson extractor")
    parser.add_argument("--dry-run", action="store_true", help="Não persiste nada")
    parser.add_argument("--tenant", default=None, help="Filtrar por tenant")
    args = parser.parse_args(argv)
    run(dry_run=args.dry_run, tenant_id=args.tenant)


if __name__ == "__main__":
    main()
