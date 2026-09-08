"""Durable tool-invocation idempotency (NXS-TOOL-001, ADR-0063).

Scoped by ``(organization_id, tool_id, idempotency_key)``. A request fingerprint (a
SHA-256 over the canonical tool key + resolved version + merged arguments) is stored with
the claim:

* SAME key + SAME fingerprint  -> the stored ToolResult is replayed (``replayed=True``);
* SAME key + DIFFERENT fingerprint -> deterministic ``NXS_TOOL_IDEMPOTENCY_CONFLICT``;
* a claim still ``PENDING`` -> retryable ``NXS_TOOL_EXECUTION_IN_PROGRESS``.

This is NOT exactly-once. A crash between the downstream call and recording ``COMPLETED``
can re-invoke a ``NON_IDEMPOTENT_WRITE`` tool once; the Integration Hub's own retry policy
already forbids retrying such an operation after it may have reached the server, and the
tool-level key is forwarded to the Hub so its idempotency layer deduplicates too.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from nexus_ai.tools.entities import ToolResult


class ToolIdempotencyStatus(StrEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ToolIdempotencyRecord:
    idempotency_key: str
    request_fingerprint: str
    status: ToolIdempotencyStatus
    result_json: dict[str, Any] | None
    error_code: str | None
    updated_at: dt.datetime


@dataclass(frozen=True, slots=True)
class ClaimOutcome:
    is_owner: bool
    existing: ToolIdempotencyRecord | None = None


class ToolIdempotencyStore(Protocol):
    async def claim(
        self,
        *,
        organization_id: UUID,
        tool_id: UUID,
        idempotency_key: str,
        request_fingerprint: str,
        expires_at: dt.datetime,
    ) -> ClaimOutcome: ...

    async def finalize(
        self,
        *,
        organization_id: UUID,
        tool_id: UUID,
        idempotency_key: str,
        status: ToolIdempotencyStatus,
        result_json: dict[str, Any] | None,
        error_code: str | None,
    ) -> None: ...

    async def get(
        self,
        *,
        organization_id: UUID,
        tool_id: UUID,
        idempotency_key: str,
    ) -> ToolIdempotencyRecord | None: ...


def request_fingerprint(tool_key: str, tool_version: int, merged_arguments: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"tool": tool_key, "version": tool_version, "arguments": merged_arguments},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def result_from_record(
    record: ToolIdempotencyRecord, *, tool_key: str, tool_version: int
) -> ToolResult:
    if record.result_json is None:  # pragma: no cover - COMPLETED always stores a result
        raise ValueError("a completed tool idempotency record has no stored result")
    restored = ToolResult.model_validate(record.result_json)
    return restored.model_copy(update={"replayed": True})
