"""Request/correlation identifier generation and validation (NXS-CTX-001).

Identifiers are lowercase hex UUIDv4 (32 characters, no dashes): collision-resistant,
URL-safe, and cheap to generate. Inbound values are accepted only when they match a
conservative allow pattern and length bound; anything else is replaced with a fresh id.
"""

from __future__ import annotations

import re
import uuid

_MAX_LEN = 128
_ALLOWED = re.compile(r"\A[A-Za-z0-9_.:-]{8,128}\Z")


def new_id() -> str:
    return uuid.uuid4().hex


def sanitize(candidate: str | None) -> str:
    """Return a trusted identifier: the candidate when acceptable, else a fresh id."""
    if candidate is None:
        return new_id()
    trimmed = candidate.strip()
    if len(trimmed) > _MAX_LEN or not _ALLOWED.match(trimmed):
        return new_id()
    return trimmed


def is_acceptable(candidate: str) -> bool:
    return bool(_ALLOWED.match(candidate.strip()))
