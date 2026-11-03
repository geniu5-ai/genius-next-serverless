"""Roda a cadeia de jobs do follow-up em memoria: cada ``schedule_in`` vira uma espera registrada."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app import jobs


def drive_follow_up(
    monkeypatch: Any,
    followup: Any,
    *args: Any,
    on_sleep: Callable[[int], None] = lambda _s: None,
) -> None:
    fila: list[tuple[Any, dict, dict]] = []
    monkeypatch.setattr(jobs, "_submit", lambda task, opts, kwargs: fila.append((task, opts, kwargs)))
    followup.start_follow_up_v2(*args)
    while fila:
        task, opts, kwargs = fila.pop(0)
        espera = opts.get("schedule_in", {}).get("seconds", 0)
        if espera:
            on_sleep(espera)
        task(**kwargs)
