"""Stable non-secret semantic identities and opaque claim tokens."""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any


def opaque_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def semantic_digest(value: dict[str, Any]) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def downstream_key(namespace: str, semantic_key: str) -> str:
    digest = hashlib.sha256(f"{namespace}:{semantic_key}".encode()).hexdigest()
    return f"human:{namespace}:{digest}"
