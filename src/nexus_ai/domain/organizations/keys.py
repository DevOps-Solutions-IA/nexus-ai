"""The immutable, globally unique, namespace-safe Organization key (NXS-ORG-002).

The key is a human-friendly handle (``clinica-san-jose``); it is NOT a security
boundary — ``organization_id`` (UUIDv7) remains canonical identity.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

from nexus_ai.core.errors import ValidationFailedError

MIN_LENGTH: Final = 3
MAX_LENGTH: Final = 48
_PATTERN: Final = re.compile(r"\A[a-z0-9](?:[a-z0-9-]{1,46}[a-z0-9])\Z")
_COLLAPSE: Final = re.compile(r"-{2,}")

RESERVED_KEYS: Final = frozenset(
    {
        "admin",
        "administrator",
        "api",
        "app",
        "auth",
        "billing",
        "console",
        "dashboard",
        "internal",
        "login",
        "nexus",
        "nexus-ai",
        "null",
        "nxs",
        "platform",
        "root",
        "static",
        "status",
        "support",
        "system",
        "undefined",
        "www",
    }
)


def _field_error(message: str) -> ValidationFailedError:
    return ValidationFailedError([{"field": "organization_key", "message": message}])


def normalize_organization_key(raw: str) -> str:
    """Deterministically normalise a candidate key to its canonical lowercase form."""
    if not isinstance(raw, str):  # pragma: no cover - typing guard
        raise _field_error("organization_key must be a string")
    text = unicodedata.normalize("NFKC", raw).strip().lower()
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in text):
        raise _field_error("organization_key must not contain control characters")
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"[^a-z0-9-]", "", text)
    text = _COLLAPSE.sub("-", text).strip("-")
    return text


def validate_organization_key(raw: str) -> str:
    key = normalize_organization_key(raw)
    if not key:
        raise _field_error("organization_key must not be empty")
    if len(key) < MIN_LENGTH or len(key) > MAX_LENGTH:
        raise _field_error(
            f"organization_key must be {MIN_LENGTH}-{MAX_LENGTH} characters after normalisation"
        )
    if not _PATTERN.match(key):
        raise _field_error("organization_key must be lowercase alphanumeric with internal hyphens")
    if key in RESERVED_KEYS:
        raise _field_error("organization_key is reserved by the platform")
    return key
