"""Canonical agent-runtime request fingerprints (NXS-P13).

The same rigour as the NXS-P11 / NXS-P12 fingerprints. Two requests that share an
``idempotency_key`` describe the SAME logical operation only when every semantic field is
identical; a change under the same key is a deterministic conflict, never a silent replay.
``organization_id`` is implicit (tenant scope + the per-organization unique key).
``correlation_id`` is observational and excluded.
"""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

AGENT_FINGERPRINT_VERSION = 1


def start_session_fingerprint(
    *,
    agent_id: UUID,
    channel: str,
    conversation_id: UUID | None,
    customer_id: UUID | None,
    call_id: UUID | None,
    voice_session_id: UUID | None,
) -> str:
    canonical = json.dumps(
        {
            "v": AGENT_FINGERPRINT_VERSION,
            "kind": "start_session",
            "agent_id": str(agent_id),
            "channel": channel,
            "conversation_id": _opt(conversation_id),
            "customer_id": _opt(customer_id),
            "call_id": _opt(call_id),
            "voice_session_id": _opt(voice_session_id),
        },
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def turn_fingerprint(*, session_id: UUID, content: str) -> str:
    canonical = json.dumps(
        {
            "v": AGENT_FINGERPRINT_VERSION,
            "kind": "turn",
            "session_id": str(session_id),
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        },
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def arguments_hash(arguments: object) -> str:
    """A stable hash of a tool-call argument object — recorded for audit instead of the
    raw arguments (which may carry customer content)."""
    canonical = json.dumps(arguments, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _opt(value: UUID | None) -> str | None:
    return None if value is None else str(value)
