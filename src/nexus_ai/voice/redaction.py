"""Redaction helpers for voice logging and events (NXS-P12).

Nothing in the voice subsystem ever logs or events a provider API key, a signed
WebSocket URL, an Authorization header, raw audio or a transcript body. These helpers
produce the ONLY shapes allowed to leave the trust boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_SECRET_HINTS = (
    "key",
    "secret",
    "token",
    "authorization",
    "password",
    "signature",
    "credential",
    "xi-api-key",
    "cookie",
    "bearer",
)
_REDACTED = "<redacted>"


def redact_mapping(data: Mapping[str, Any]) -> dict[str, str]:
    """Return a shallow, string-valued copy with any secret-looking key redacted and
    every value bounded to 200 chars. Bytes values are replaced with a size marker."""
    out: dict[str, str] = {}
    for key, value in data.items():
        low = str(key).lower()
        if any(hint in low for hint in _SECRET_HINTS):
            out[str(key)] = _REDACTED
            continue
        if isinstance(value, bytes | bytearray):
            out[str(key)] = f"<bytes:{len(value)}>"
            continue
        out[str(key)] = str(value)[:200]
    return out


def redact_ws_url(url: str) -> str:
    """A signed WebSocket URL carries auth in its query string — log only the origin."""
    without_query = url.split("?", 1)[0]
    return without_query.split("#", 1)[0]


def safe_session_log_fields(
    *,
    organization_id: Any,
    session_id: Any,
    call_id: Any,
    media_session_id: Any,
    provider: str,
    state: str,
    error_code: str | None = None,
    correlation_id: str | None = None,
) -> dict[str, str]:
    """The fixed, safe field set for a voice-session log line."""
    fields = {
        "organization_id": str(organization_id),
        "voice_session_id": str(session_id),
        "call_id": str(call_id),
        "media_session_id": str(media_session_id),
        "provider": provider,
        "state": state,
    }
    if error_code is not None:
        fields["error_code"] = error_code
    if correlation_id is not None:
        fields["correlation_id"] = correlation_id
    return fields
