"""Security adapter: deterministic local guard + HTTP contract for a guard sidecar.

Scope note
----------
The main Genius-Next runtime is Python 3.14.  The heavy content guard (``llm_guard``)
needs a Python 3.12 sidecar, so this module is deliberately a **contract/adaptor**:
* ``scan_invisible_text`` — a deterministic, dependency-free local check (Unicode
  invisible control characters) that always works on the current runtime.
* ``GuardClient.assess`` — a thin HTTP client that calls ``POST <base>/v1/scan`` and
  validates the sidecar's JSON reply, degrading to ``unavailable`` without raising.

No credentials, tokens or request payloads are ever logged or echoed into findings.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from collections.abc import Sequence
from enum import Enum
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# Unicode ranges that denote "invisible/decorative" text editing marks:
#  * category Cf  — format characters (U+200B zero-width space, bidi controls, ...)
#  * U+E0000..U+E007F — the "Tags" block (variation/emoji tag marks).
_INVISIBLE_CF_CATEGORY = "Cf"
_TAGS_RANGE = range(0xE0000, 0xE0080)


def _is_invisible_tag(ch: str) -> bool:
    return ord(ch) in _TAGS_RANGE


# 08/out/2026: Cf que o proprio WhatsApp e os teclados poem em texto comum.
# LRM/RLM (U+200E/U+200F) chegam colados em mensagem de paciente e o ZWJ
# (U+200D) monta emoji composto ("mulher levantando a mao", "medica"). Bloquear
# esses dava BLOCK deterministico em fala normal e, na SAIDA, trocava a resposta
# da atendente pelo fallback da equipe. Os vetores de contrabando continuam
# barrados: bloco Tags, overrides bidi (U+202A-U+202E, U+2066-U+2069), largura
# zero solta (U+200B, U+2060, U+FEFF) e ZWJ fora de emoji. Os caracteres vao
# sempre como escape: colado no fonte, um editor que limpa invisiveis apagaria
# a excecao em silencio.
_BENIGN_DIRECTION_MARKS = frozenset({"\u200e", "\u200f"})  # LRM, RLM
_ZWJ = "\u200d"
_VARIATION_SELECTOR_16 = "\ufe0f"
_SKIN_TONE_RANGE = range(0x1F3FB, 0x1F400)
# O WhatsApp poe 1 ou 2 LRM/RLM numa mensagem. Sem teto, uma fila de LRM/RLM
# alternados vira canal binario invisivel (e a mesma checagem protege a base de
# conhecimento); acima disso volta a ser BLOCK.
_MAX_DIRECTION_MARKS = 4


def _is_emoji_char(ch: str) -> bool:
    return unicodedata.category(ch) == "So"


def _is_emoji_zwj(text: str, index: int) -> bool:
    """ZWJ ligando dois emojis (sequencia de emoji), nunca ZWJ entre letras."""
    if index == 0 or index + 1 >= len(text):
        return False
    before = text[index - 1]
    after = text[index + 1]
    before_ok = (
        _is_emoji_char(before)
        or before == _VARIATION_SELECTOR_16
        or ord(before) in _SKIN_TONE_RANGE
    )
    return before_ok and _is_emoji_char(after)


def _is_benign_format_char(text: str, index: int) -> bool:
    ch = text[index]
    if ch in _BENIGN_DIRECTION_MARKS:
        return True
    return ch == _ZWJ and _is_emoji_zwj(text, index)


# --------------------------------------------------------------------------- #
# Public contracts
# --------------------------------------------------------------------------- #
class GuardStage(str, Enum):
    """Where the guard runs in the pipeline."""

    INPUT = "input"
    OUTPUT = "output"


class GuardDisposition(str, Enum):
    """Overall outcome of a guard assessment."""

    ALLOW = "allow"
    BLOCK = "block"
    UNAVAILABLE = "unavailable"


class GuardFinding(BaseModel):
    """A single root-cause finding.  ``reason_code`` is always an **opaque code** —
    never a snippet of client text — so findings are safe to log."""

    model_config = ConfigDict(extra="forbid")

    scanner: str
    reason_code: str
    score: float = Field(default=0.0, ge=0.0, le=1.0)


class GuardAssessment(BaseModel):
    """Result of scanning one stage.

    ``deterministic`` is True when the outcome came from the local, reproducible
    check rather than the (statistical) sidecar.
    """

    model_config = ConfigDict(extra="forbid")

    stage: Literal["input", "output"]
    disposition: Literal["allow", "block", "unavailable"]
    findings: list[GuardFinding]
    event_refs: tuple[str, ...] = ()
    deterministic: bool


# --------------------------------------------------------------------------- #
# Deterministic local check
# --------------------------------------------------------------------------- #
def scan_invisible_text(text: str, *, event_refs: Sequence[str] = ()) -> GuardAssessment:
    """Detect invisible/format Unicode in *text* without any network or heavy deps.

    Straightforward text returns ``allow``; any Unicode category ``Cf`` character or
    any codepoint in the Tags plane (U+E0000..U+E007F) returns ``block`` with a
    deterministic finding.  Exceptions (benign WhatsApp/keyboard marks): up to
    ``_MAX_DIRECTION_MARKS`` LRM/RLM per text and a ZWJ joining two emoji.  The finding carries an opaque ``reason_code``
    only and never surfaces the offending client text.
    """
    refs = tuple(event_refs)
    direction_marks = sum(1 for ch in text if ch in _BENIGN_DIRECTION_MARKS)
    if direction_marks > _MAX_DIRECTION_MARKS:
        return GuardAssessment(
            stage=GuardStage.INPUT.value,
            disposition=GuardDisposition.BLOCK.value,
            findings=[
                GuardFinding(
                    scanner="security_guard.local",
                    reason_code="unicode_invisible:direction_marks",
                    score=1.0,
                )
            ],
            event_refs=refs,
            deterministic=True,
        )
    for index, ch in enumerate(text):
        if unicodedata.category(
            ch
        ) == _INVISIBLE_CF_CATEGORY and not _is_benign_format_char(text, index):
            return GuardAssessment(
                stage=GuardStage.INPUT.value,
                disposition=GuardDisposition.BLOCK.value,
                findings=[
                    GuardFinding(
                        scanner="security_guard.local",
                        reason_code="unicode_invisible:cf",
                        score=1.0,
                    )
                ],
                event_refs=refs,
                deterministic=True,
            )
        if _is_invisible_tag(ch):
            return GuardAssessment(
                stage=GuardStage.INPUT.value,
                disposition=GuardDisposition.BLOCK.value,
                findings=[
                    GuardFinding(
                        scanner="security_guard.local",
                        reason_code="unicode_invisible:tags",
                        score=1.0,
                    )
                ],
                event_refs=refs,
                deterministic=True,
            )

    return GuardAssessment(
        stage=GuardStage.INPUT.value,
        disposition=GuardDisposition.ALLOW.value,
        findings=[],
        event_refs=refs,
        deterministic=True,
    )


# --------------------------------------------------------------------------- #
# HTTP adaptor for the guard sidecar
# --------------------------------------------------------------------------- #
class GuardClient:
    """Small client for the guard sidecar's ``POST /v1/scan`` endpoint.

    The client never touches ``llm_guard`` and never logs request payloads, the
    base URL, or any token/credential.  On timeouts, HTTP/transport errors, or an
    invalid response schema it returns an ``unavailable`` assessment instead of
    raising, so callers can fail safe.

    If the deterministic ``scan_invisible_text`` already blocks one of the texts,
    :meth:`assess` short-circuits to that block without any network call.
    """

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def assess(
        self,
        stage: GuardStage,
        texts: Sequence[str],
        *,
        event_refs: Sequence[str] = (),
    ) -> GuardAssessment:
        stage_value = GuardStage(stage)
        refs = tuple(event_refs)

        # Fast, deterministic local path: if anything is already blocked on the
        # invisible-text check, do not spend an HTTP round trip — block locally.
        # A local block still reports the requested stage: callers must hear back
        # about the stage they asked to assess, never a silently shifted input one.
        for text in texts:
            local = scan_invisible_text(text, event_refs=refs)
            if local.disposition == GuardDisposition.BLOCK:
                return local.model_copy(update={"stage": stage_value.value})

        return self._call_sidecar(stage_value, list(texts), refs)

    def _call_sidecar(
        self, stage: GuardStage, texts: list[str], refs: tuple[str, ...]
    ) -> GuardAssessment:
        try:
            resp = self._client.post(
                f"{self._base_url}/v1/scan",
                json={"stage": stage.value, "texts": texts, "event_refs": list(refs)},
            )
            resp.raise_for_status()
            payload = json.loads(resp.text)
            return self._validate_response(stage, payload, refs)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            # Timeout, transport/HTTP error, malformed JSON or an unexpected schema
            # shape: fail safe as unavailable.  Log a safe, opaque message only —
            # never exc_info, which can carry the URL, error text or response body.
            logger.warning("guard sidecar unavailable")
            return GuardAssessment(
                stage=stage.value,
                disposition=GuardDisposition.UNAVAILABLE.value,
                findings=[
                    GuardFinding(
                        scanner="security_guard.client",
                        reason_code="guard_unavailable",
                        score=0.0,
                    )
                ],
                event_refs=refs,
                deterministic=False,
            )

    def _validate_response(
        self, stage: GuardStage, payload: object, refs: tuple[str, ...]
    ) -> GuardAssessment:
        if not isinstance(payload, dict):
            raise ValueError("guard response must be a JSON object")

        seen_stage = payload.get("stage")
        disposition = payload.get("disposition")
        raw_findings = payload.get("findings")
        seen_refs = payload.get("event_refs")

        # Strictness: every required field must be present; unknown disposition and
        # findings that are not a list also fail validation.  A reply for a
        # different stage than the one requested is a crossed/stale response and
        # must never be accepted.
        if seen_stage not in (GuardStage.INPUT, GuardStage.OUTPUT):
            raise ValueError("guard response stage is invalid")
        if GuardStage(seen_stage) is not stage:
            raise ValueError("guard response stage does not match the request")
        if disposition not in (
            GuardDisposition.ALLOW,
            GuardDisposition.BLOCK,
            GuardDisposition.UNAVAILABLE,
        ):
            raise ValueError("guard response disposition is invalid")
        if not isinstance(raw_findings, list):
            raise ValueError("guard response findings missing")
        if not isinstance(seen_refs, list):
            raise ValueError("guard response event_refs missing")

        return GuardAssessment(
            stage=GuardStage(seen_stage).value,
            disposition=GuardDisposition(disposition).value,
            findings=[GuardFinding.model_validate(f) for f in raw_findings],
            event_refs=tuple(str(r) for r in seen_refs),
            deterministic=False,
        )
