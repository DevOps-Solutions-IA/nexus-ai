"""Canonical voice-session request fingerprint (NXS-P12).

The same rigour as the NXS-P11 outbound-call fingerprint (ADR-0086). Two ``start_session``
requests that share an ``idempotency_key`` describe the SAME logical voice session only
when every field that defines the session is identical. All idempotency paths compare
exactly this one value — never a partial subset.

Semantic fields (a change to any of these under the same key is
``NXS_VOICE_IDEMPOTENCY_CONFLICT``, never a silent replay):

* ``media_session_id``     — the ACTIVE NXS-P11 media session the voice stream attaches to
* ``provider_account_id``  — which provider account runs the session
* ``voice_profile_id``     — the Organization-owned voice configuration resource
* normalized ``options``   — provider-neutral session options that change provider behaviour

``organization_id`` is implicit (tenant scope + the ``(organization_id, idempotency_key)``
unique key). ``correlation_id`` is OBSERVATIONAL — a trace-propagation hint that never
changes the session — and is excluded.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from uuid import UUID

#: Bump when the canonical serialization changes.
VOICE_FINGERPRINT_VERSION = 1


def voice_session_fingerprint(
    *,
    media_session_id: UUID,
    provider_account_id: UUID,
    voice_profile_id: UUID,
    options: Mapping[str, str],
) -> str:
    """A deterministic hex SHA-256 over the canonical form of the semantic fields."""
    canonical = json.dumps(
        {
            "v": VOICE_FINGERPRINT_VERSION,
            "media_session_id": str(media_session_id),
            "provider_account_id": str(provider_account_id),
            "voice_profile_id": str(voice_profile_id),
            "options": {key: options[key] for key in sorted(options)},
        },
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
