"""Deterministic non-secret Campaign identities."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID


def semantic_digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def recipient_identity(
    organization_id: UUID,
    campaign_id: UUID,
    revision: int,
    run_id: UUID,
    recipient_id: UUID,
    channel: str,
) -> str:
    return semantic_digest(
        {
            "organization_id": str(organization_id),
            "campaign_id": str(campaign_id),
            "revision": revision,
            "run_id": str(run_id),
            "recipient_id": str(recipient_id),
            "channel": channel,
        }
    )


def downstream_key(prefix: str, logical_identity: str) -> str:
    return f"campaign:{prefix}:{logical_identity}"
