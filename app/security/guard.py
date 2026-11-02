"""Security guard: deterministic local checks, no network and no sidecar.

* ``scan_invisible_text`` — Unicode invisible control characters (both stages).
* ``contains_secret`` — credential patterns from ``pydantic_ai_harness`` detectors.
* ``assess_locally`` — one stage assessment built from the two checks above.

No credentials, tokens or request payloads are ever logged or echoed into findings.
The output guardrail of the reply agent itself lives in ``app/reply/guardrails.py``.
"""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Sequence
from enum import Enum
from functools import lru_cache
from typing import Literal

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
# Avaliacao local (substitui o sidecar llm-guard, 02/nov/2026)
# --------------------------------------------------------------------------- #
# O sidecar llm-guard (Python 3.12, modelo de ML na porta 8722) saiu: na entrada
# o achado de prompt injection ja era so consultivo desde 08/out/2026 (marcava
# fala normal de paciente) e, na saida, ele so procurava segredos. Sem o
# sidecar, a guarda de saida falhava FECHADO por indisponibilidade e trocava
# toda resposta pelo handoff seguro (medido no Render em 02/nov/2026).
#
# Agora tudo e local, deterministico e sem rede, entao nunca fica
# "unavailable": texto invisivel nos dois estagios e, na saida, os padroes de
# credencial do pydantic-ai-harness (chaves OpenAI/Anthropic/AWS/Stripe/Google,
# tokens GitHub/Slack, JWT, chave privada).  O valor ``unavailable`` continua
# no contrato porque linhas antigas de security_events o carregam.
_SECRET_SCANNER = "pydantic_ai_harness.secret_data"


@lru_cache(maxsize=1)
def _secret_detector():
    from pydantic_ai_harness.guardrails import detectors

    return detectors.secret_data()


def contains_secret(text: str) -> bool:
    """True quando o texto tem algo com cara de credencial (chave, token, JWT)."""
    if not text:
        return False
    return _secret_detector()(text).action == "replace"


def assess_locally(
    stage: GuardStage | str,
    texts: Sequence[str],
    *,
    event_refs: Sequence[str] = (),
) -> GuardAssessment:
    """Avalia um estagio sem rede: invisivel nos dois, segredo so na saida.

    Os achados levam so codigo opaco, nunca trecho do texto avaliado.
    """
    stage_value = GuardStage(stage)
    refs = tuple(event_refs)
    for text in texts:
        local = scan_invisible_text(text, event_refs=refs)
        if local.disposition == GuardDisposition.BLOCK:
            return local.model_copy(update={"stage": stage_value.value})
    if stage_value is GuardStage.OUTPUT and any(contains_secret(t) for t in texts):
        return GuardAssessment(
            stage=stage_value.value,
            disposition=GuardDisposition.BLOCK.value,
            findings=[
                GuardFinding(
                    scanner=_SECRET_SCANNER,
                    reason_code="secret_in_output",
                    score=1.0,
                )
            ],
            event_refs=refs,
            deterministic=True,
        )
    return GuardAssessment(
        stage=stage_value.value,
        disposition=GuardDisposition.ALLOW.value,
        findings=[],
        event_refs=refs,
        deterministic=True,
    )
