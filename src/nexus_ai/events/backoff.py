"""Bounded exponential backoff (NXS-EVENT-007).

Pure, deterministic and unit-tested. ``attempt`` is 1-based (the first retry is attempt
1). The delay never exceeds ``max_delay`` and a poison message therefore retries at a
bounded cadence forever only until ``max_attempts`` — after which it is dead-lettered,
never hot-looped.
"""

from __future__ import annotations

_MAX_EXPONENT = 20


def backoff_delay(*, attempt: int, base_seconds: float, max_seconds: float) -> float:
    if attempt < 1:
        return 0.0
    exponent = min(attempt - 1, _MAX_EXPONENT)
    return min(max_seconds, base_seconds * (2.0**exponent))


def should_dead_letter(*, attempt: int, max_attempts: int) -> bool:
    return attempt >= max_attempts
