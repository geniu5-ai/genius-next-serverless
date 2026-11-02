"""agent lessons — self-learning RAG for tactical knowledge.

Storage and retrieval of lessons extracted from closed conversations.
Tenant-aware, segmentable, pgvector-backed.  Only storage + search;
generation workflow is a separate future task.
"""

# TODO(lessons-generation): job que analisa conversas fechadas e
# chama save_lesson() — task futura (prompt de extração, cadência, dedup).

from __future__ import annotations

import logging
import os
import re
import unicodedata

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text as _sql_text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_INTERNAL = ConfigDict(extra="forbid", str_strip_whitespace=True)

# ── Config ──────────────────────────────────────────────────────────────────

_JINA_EMBEDDINGS_URL = "https://api.jina.ai/v1/embeddings"
_JINA_MODEL = "jina-embeddings-v3"
_JINA_DIMENSIONS = 384
_JINA_TASK = "retrieval.passage"
_USER_AGENT = "genius-next-lessons/1.0"
# 20/out/2026: o Jina ficou sem chave/saldo e a extracao de licoes parou em silencio (TRAPS #70).
# Backend padrao agora: servico local genius-embed (multilingual-e5-small, 384 dims, sem rede externa).
_LOCAL_EMBED_URL = "http://127.0.0.1:8721/embed"
_LOCAL_EMBED_MODEL = "multilingual-e5-small"


def _embed_backend() -> str:
    return os.environ.get("LESSON_EMBED_BACKEND", "local").strip().lower()


def active_embedding_model() -> str:
    return _JINA_MODEL if _embed_backend() == "jina" else _LOCAL_EMBED_MODEL
_DEDUP_SIMILARITY_THRESHOLD = 0.92
_FAILED_CREW_RESULT_AGENT = "genius-next-lesson-extractor"

# ── Pydantic models ─────────────────────────────────────────────────────────


class LessonResult(BaseModel):
    model_config = _INTERNAL

    found: bool
    id: int
    content: str
    segmento: str | None = None
    confidence: float | None = None


class LessonSearchResult(BaseModel):
    model_config = _INTERNAL

    lessons: list[LessonResult] = Field(default_factory=list)
    total: int = 0


# ── SQL ─────────────────────────────────────────────────────────────────────

_INSERT_LESSON_SQL = _sql_text(
    """
    /* agent.insert_lesson */
    INSERT INTO genius_next.lessons
        (tenant_id, segmento, content, embedding, embedding_model,
         source_conversation_id, confidence)
    VALUES
        (:tenant_id, :segmento, :content, :embedding, :embedding_model,
         :source_conversation_id, :confidence)
    RETURNING id
    """
)

_SEARCH_LESSONS_SQL = _sql_text(
    """
    /* agent.search_lessons */
    SELECT id, content, segmento, confidence,
           embedding <=> CAST(:query_vec AS vector) AS distance
    FROM genius_next.lessons
    WHERE tenant_id = :tenant_id
      AND (segmento = :segmento OR segmento IS NULL)
    ORDER BY distance
    LIMIT :limit
    """
)

_SIMILAR_LESSON_SQL = _sql_text(
    """
    /* agent.similar_lesson */
    SELECT id, content, segmento, confidence,
           embedding <=> CAST(:embedding AS vector) AS distance
    FROM genius_next.lessons
    WHERE tenant_id = :tenant_id
      AND embedding IS NOT NULL
    ORDER BY distance
    LIMIT 1
    """
)

_CREW_RESULTS_COLUMNS_SQL = _sql_text(
    """
    /* agent.crew_results_columns */
    SELECT column_name, is_nullable, column_default
    FROM information_schema.columns
    WHERE table_schema = 'crew'
      AND table_name = 'results'
    ORDER BY ordinal_position
    """
)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _load_api_key() -> str:
    """Load JINA_API_KEY from ~/.config/genius/tokens.local."""
    tokens_path = os.path.expanduser("~/.config/genius/tokens.local")
    try:
        with open(tokens_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        raise RuntimeError(f"Missing tokens file: {tokens_path}")
    api_key = None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key == "JINA_API_KEY":
            api_key = value
            break
    if api_key is None:
        raise RuntimeError("JINA_API_KEY not found in tokens.local")
    return api_key


def _normalize_segmento_text(text: str) -> str:
    """Normalize accents/case so the regex map stays simple."""
    normalized = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return stripped.lower()


_SEGMENTO_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "implante",
        (
            r"\bimplante(?:s)?\b",
            r"\bimplantacao\b",
            r"\bimplantar\b",
        ),
    ),
    (
        "aparelho",
        (
            r"\baparelh(?:o|os)\b",
            r"\bortodont(?:ico|ia|ista)\b",
        ),
    ),
    (
        "protese",
        (
            r"\bprotese(?:s)?\b",
            r"\bprótese(?:s)?\b",
        ),
    ),
    (
        "clareamento",
        (
            r"\bclareament(?:o|os|a)\b",
            r"\bclarear\b",
            r"\bbranqueament(?:o|os|a)\b",
        ),
    ),
    (
        "limpeza",
        (
            r"\blimpeza(?:s)?\b",
            r"\bprofilaxia\b",
            r"\braspagem\b",
            r"\bprotocolo\b",
        ),
    ),
    (
        "canal",
        (
            r"\bcanal(?:es)?\b",
        ),
    ),
    (
        "extracao",
        (
            r"\bextraca(?:o|oes)\b",
            r"\bextrair\b",
            r"\bsiso(?:s)?\b",
            r"\bdente do juizo\b",
        ),
    ),
    (
        "avaliacao",
        (
            r"\bavaliaca(?:o|oes)\b",
            r"\bavaliar\b",
        ),
    ),
)


def infer_segmento(text: str) -> str | None:
    """Infer the lesson segmento from the conversation text.

    Simple regex-based classification using the same dentistry vocabulary that
    appears in the lead summary schema: implante, aparelho, protese,
    clareamento, limpeza, canal, extracao, avaliacao.
    """
    normalized = _normalize_segmento_text(text)
    for segmento, patterns in _SEGMENTO_PATTERNS:
        for pattern in patterns:
            if re.search(pattern, normalized):
                return segmento
    return None


def _is_similar_lesson(
    session: Session,
    *,
    tenant_id: str,
    embedding: list[float],
) -> tuple[bool, float | None, int | None]:
    """Return whether the new lesson is too similar to an existing one."""
    result = session.execute(
        _SIMILAR_LESSON_SQL,
        {"tenant_id": tenant_id, "embedding": embedding},
    )
    row = result.mappings().first()
    if row is None:
        return False, None, None
    distance = float(row["distance"])
    similarity = 1.0 - distance
    return similarity > _DEDUP_SIMILARITY_THRESHOLD, similarity, row["id"]


def _record_failed_lesson_result(
    session: Session,
    *,
    tenant_id: str,
    segmento: str | None,
    content: str,
    error: Exception,
) -> None:
    """Best-effort crew.results write so embedding failures show up in monitoring."""
    bind = session.get_bind()
    if bind is None:
        logger.warning("crew result write skipped: session has no database bind")
        return

    body = (
        f"save_lesson failed for tenant={tenant_id} segmento={segmento!r}. "
        f"content={content[:240]!r}. error={error!s}"
    )

    try:
        with bind.begin() as conn:
            rows = conn.execute(_CREW_RESULTS_COLUMNS_SQL).mappings().all()
            columns = {row["column_name"]: row for row in rows}
            if not columns:
                logger.warning("crew result write skipped: crew.results not available")
                return

            payload: dict[str, object] = {}
            for key, value in {
                "agent": _FAILED_CREW_RESULT_AGENT,
                "status": "failed",
                "body": body,
                "message": body,
                "summary": body,
                "details": body,
                "tenant_id": tenant_id,
                "segmento": segmento,
            }.items():
                if key in columns and value is not None:
                    payload[key] = value

            required_missing = [
                name
                for name, meta in columns.items()
                if meta["is_nullable"] == "NO"
                and meta["column_default"] is None
                and name not in payload
            ]
            if required_missing:
                logger.warning(
                    "crew result write skipped: missing required columns %s",
                    required_missing,
                )
                return

            if not payload:
                logger.warning("crew result write skipped: no matching columns")
                return

            insert_sql = _sql_text(
                "INSERT INTO crew.results ("
                + ", ".join(payload)
                + ") VALUES ("
                + ", ".join(f":{name}" for name in payload)
                + ")"
            )
            conn.execute(insert_sql, payload)
    except Exception:
        logger.warning("crew result write failed", exc_info=True)


def _embed_local(text: str, *, query: bool) -> list[float]:
    """Embedding local (e5 exige o prefixo passage:/query:); levanta em qualquer falha."""
    url = os.environ.get("LESSON_EMBED_URL", _LOCAL_EMBED_URL)
    prefixed = ("query: " if query else "passage: ") + text.strip()
    with httpx.Client(timeout=30.0) as client:
        response = client.post(url, json={"texts": [prefixed]})
    if response.status_code != 200:
        raise RuntimeError(f"local embed error {response.status_code}")
    vectors = response.json().get("vectors") or []
    if not vectors or len(vectors[0]) != _JINA_DIMENSIONS:
        raise RuntimeError("local embed returned an unexpected vector")
    return list(vectors[0])


def embed_text(text: str, *, query: bool = False) -> list[float]:
    """Embed text: servico local (padrao) ou Jina Embeddings v3 (384d) com LESSON_EMBED_BACKEND=jina.

    POST https://api.jina.ai/v1/embeddings with custom User-Agent
    (Cloudflare WAF rejects default Python UA with 403 code 1010).
    No local fallback — propagate errors.
    """
    if _embed_backend() != "jina":
        return _embed_local(text, query=query)
    api_key = _load_api_key()
    payload = {
        "model": _JINA_MODEL,
        "task": _JINA_TASK,
        "dimensions": _JINA_DIMENSIONS,
        "late_chunking": False,
        "embedding_type": "float",
        "input": [text],
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": _USER_AGENT,
    }
    with httpx.Client(timeout=30.0) as client:
        response = client.post(_JINA_EMBEDDINGS_URL, json=payload, headers=headers)
    if response.status_code != 200:
        raise RuntimeError(f"Jina API error {response.status_code}: {response.text}")
    data = response.json()
    embedding = data["data"][0]["embedding"]
    if not embedding:
        raise RuntimeError("Jina API returned an empty embedding")
    return embedding


# ── Public API ──────────────────────────────────────────────────────────────


def save_lesson(
    session: Session,
    *,
    tenant_id: str,
    segmento: str | None,
    content: str,
    source_conversation_id: int | None = None,
    confidence: float | None = None,
) -> LessonResult | None:
    """Embed content and persist a lesson in the RAG store.

    Returns the saved lesson with its generated id, or ``None`` when the
    lesson is skipped because it is too similar to an existing lesson in the
    same tenant.
    """
    try:
        embedding = embed_text(content)
        is_similar, similarity, similar_id = _is_similar_lesson(
            session,
            tenant_id=tenant_id,
            embedding=embedding,
        )
        if is_similar:
            logger.info(
                "lesson skipped by dedup: tenant=%s segmento=%s similar_to=%s similarity=%.3f",
                tenant_id,
                segmento,
                similar_id,
                similarity or 0.0,
            )
            return None

        result = session.execute(
            _INSERT_LESSON_SQL,
            {
                "tenant_id": tenant_id,
                "segmento": segmento,
                "content": content,
                "embedding": embedding,
                "embedding_model": active_embedding_model(),
                "source_conversation_id": source_conversation_id,
                "confidence": confidence,
            },
        )
        lesson_id = result.scalar_one()
        return LessonResult(
            found=True,
            id=lesson_id,
            content=content,
            segmento=segmento,
            confidence=confidence,
        )
    except Exception as exc:
        logger.error(
            "lesson embedding/save failed for tenant=%s segmento=%s content=%r",
            tenant_id,
            segmento,
            content,
            exc_info=True,
        )
        _record_failed_lesson_result(
            session,
            tenant_id=tenant_id,
            segmento=segmento,
            content=content,
            error=exc,
        )


def search_lessons(
    session: Session,
    *,
    tenant_id: str,
    segmento: str | None,
    query: str,
    limit: int = 4,
) -> LessonSearchResult:
    """Semantic search over lessons for a given tenant and segment.

    Embedds the query, then finds the most similar lessons.
    Falls back to segmento=NULL (generic) lessons if no segmento-specific match.
    """
    try:
        query_vec = embed_text(query, query=True)
        result = session.execute(
            _SEARCH_LESSONS_SQL,
            {
                "tenant_id": tenant_id,
                "segmento": segmento,
                "query_vec": query_vec,
                "limit": limit,
            },
        )
        rows = result.mappings().all()
        lessons = []
        for row in rows:
            lessons.append(
                LessonResult(
                    found=True,
                    id=row["id"],
                    content=row["content"],
                    segmento=row["segmento"],
                    confidence=row["confidence"],
                )
            )
        return LessonSearchResult(lessons=lessons, total=len(lessons))
    except Exception:
        logger.warning("lesson search failed", exc_info=True)
        return LessonSearchResult(lessons=[], total=0)
