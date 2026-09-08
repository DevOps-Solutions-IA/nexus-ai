"""Durable execution idempotency (NXS-INT-001, ADR-0060).

Scoped by ``(organization_id, integration_id, operation_key, idempotency_key)``. A
request fingerprint (a SHA-256 over the canonical operation key + input) is stored with
the claim so that:

* the SAME key with the SAME request replays the stored result (``replayed=True``);
* the SAME key with a DIFFERENT request is a deterministic
  ``NXS_INT_IDEMPOTENCY_CONFLICT``;
* a claim still ``PENDING`` (an execution in flight, or a crash) yields a retryable
  ``NXS_INT_EXECUTION_IN_PROGRESS`` until it is finalised or its retention window lapses.

This is NOT exactly-once execution — a crash between "send" and "record COMPLETED" can
still re-send a ``NON_IDEMPOTENT`` operation once. The retry policy already forbids
retrying such operations after they may have reached the server; idempotency adds durable
replay on top.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from nexus_ai.integrations.entities import IntegrationResult


class IdempotencyStatus(StrEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    idempotency_key: str
    request_fingerprint: str
    status: IdempotencyStatus
    result_json: dict[str, Any] | None
    error_code: str | None
    updated_at: dt.datetime


@dataclass(frozen=True, slots=True)
class ClaimOutcome:
    #: True when this caller won the claim and must now execute.
    is_owner: bool
    existing: IdempotencyRecord | None = None


class IdempotencyStore(Protocol):
    async def claim(
        self,
        *,
        organization_id: UUID,
        integration_id: UUID,
        operation_key: str,
        idempotency_key: str,
        request_fingerprint: str,
        expires_at: dt.datetime,
    ) -> ClaimOutcome: ...

    async def finalize(
        self,
        *,
        organization_id: UUID,
        integration_id: UUID,
        operation_key: str,
        idempotency_key: str,
        status: IdempotencyStatus,
        result_json: dict[str, Any] | None,
        error_code: str | None,
    ) -> None: ...

    async def get(
        self,
        *,
        organization_id: UUID,
        integration_id: UUID,
        operation_key: str,
        idempotency_key: str,
    ) -> IdempotencyRecord | None: ...


def request_fingerprint(operation_key: str, raw_input: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"op": operation_key, "input": raw_input},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def result_from_record(
    record: IdempotencyRecord, *, integration_id: UUID, operation_key: str
) -> IntegrationResult:
    if record.result_json is None:  # pragma: no cover - COMPLETED always stores a result
        raise ValueError("a completed idempotency record has no stored result")
    restored = IntegrationResult.model_validate(record.result_json)
    return restored.model_copy(update={"replayed": True})
