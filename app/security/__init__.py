"""Security guard module for Genius Next.

`app.security` exposes small public models (``GuardStage``, ``GuardDisposition``,
``GuardFinding``, ``GuardAssessment``) and deterministic local checks
(``scan_invisible_text``, ``contains_secret``, ``assess_locally``).

There is no guard sidecar anymore (02/nov/2026): the llm-guard runtime on port 8722
was removed and every check here is pure Python, so a stage can never come back
``unavailable``.  The reply agent's own output guardrail (catalog prices) lives in
``app/reply/guardrails.py``.
"""

from app.security.guard import (
    GuardAssessment,
    GuardDisposition,
    GuardFinding,
    GuardStage,
    assess_locally,
    contains_secret,
    scan_invisible_text,
)

__all__ = [
    "GuardAssessment",
    "GuardDisposition",
    "GuardFinding",
    "GuardStage",
    "assess_locally",
    "contains_secret",
    "scan_invisible_text",
]
