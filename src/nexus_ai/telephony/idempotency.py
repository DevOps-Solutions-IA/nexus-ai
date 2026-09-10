"""Canonical outbound-call request fingerprint (NXS-P11: NXS-TEL-001).

Two ``create_call`` requests that share an ``idempotency_key`` describe the SAME logical
call only when every field that defines the call is identical. The fingerprint binds
them so all four idempotency paths (early replay, top-of-transaction replay,
``IntegrityError`` winner resolution, bounded-retry winner lookup) compare exactly the
same value — never a partial subset.

Semantic fields (a change to any of these under the same key is
``NXS_TELEPHONY_IDEMPOTENCY_CONFLICT``, never a silent replay):

* ``provider_account_id`` — which trunk / provider account places the call
* ``from_number_id``      — the Organization-owned caller-ID number (a changed caller ID
  must never be replayed onto the original call)
* canonical ``destination`` — the routing target after E.164 / SIP-alias canonicalization
* normalized ``metadata``  — carried into :class:`OutboundCallSpec` and handed to the
  provider adapter, so it can influence provider behaviour

``organization_id`` is implicit (tenant scope + the ``(organization_id, idempotency_key)``
unique key). ``correlation_id`` is OBSERVATIONAL — a trace-propagation hint that never
changes the call that is placed — and is deliberately excluded from the fingerprint.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from uuid import UUID

#: Bump when the canonical serialization changes so old and new fingerprints never
#: collide across a deploy.
OUTBOUND_FINGERPRINT_VERSION = 1


def outbound_call_fingerprint(
    *,
    provider_account_id: UUID,
    from_number_id: UUID,
    destination: str,
    metadata: Mapping[str, str],
) -> str:
    """A deterministic hex SHA-256 over the canonical form of the semantic fields."""
    canonical = json.dumps(
        {
            "v": OUTBOUND_FINGERPRINT_VERSION,
            "provider_account_id": str(provider_account_id),
            "from_number_id": str(from_number_id),
            "destination": destination,
            "metadata": {key: metadata[key] for key in sorted(metadata)},
        },
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
