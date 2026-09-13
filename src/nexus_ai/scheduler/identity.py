"""Stable identities for durable Scheduler occurrences."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid

OCCURRENCE_KEY_VERSION = "v1"


def build_occurrence_key(
    *,
    schedule_id: uuid.UUID,
    schedule_revision: int,
    timezone: str,
    intended_local_time: dt.datetime,
    fold: int,
) -> str:
    if schedule_revision < 1:
        raise ValueError("schedule_revision must be positive")
    if intended_local_time.tzinfo is not None:
        raise ValueError("intended_local_time must be a naive local wall-clock value")
    if fold not in {0, 1}:
        raise ValueError("fold must be 0 or 1")
    canonical = json.dumps(
        {
            "fold": fold,
            "intended_local_time": intended_local_time.isoformat(timespec="microseconds"),
            "schedule_id": str(schedule_id),
            "schedule_revision": schedule_revision,
            "timezone": timezone,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{OCCURRENCE_KEY_VERSION}:r{schedule_revision}:f{fold}:{digest}"
