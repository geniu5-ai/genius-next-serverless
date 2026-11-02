"""Security guard module for Genius Next.

`app.security` is the *contract/adaptor* layer: it exposes public, small models
(``GuardStage``, ``GuardDisposition``, ``GuardFinding``, ``GuardAssessment``), a
deterministic local sanitizer (``scan_invisible_text``) and ``GuardClient``, an
HTTP adaptor for the heavy ``llm_guard`` runtime that lives on a future Python 3.12
sidecar.

This package never imports ``llm_guard`` — the main runtime here is Python 3.14, and
installing the native sidecar deps there is out of scope.  Everything in here is pure
Python + the already-available ``pydantic`` / ``httpx``.
"""

from app.security.guard import (
    GuardAssessment,
    GuardClient,
    GuardDisposition,
    GuardFinding,
    GuardStage,
    scan_invisible_text,
)

__all__ = [
    "GuardAssessment",
    "GuardClient",
    "GuardDisposition",
    "GuardFinding",
    "GuardStage",
    "scan_invisible_text",
]