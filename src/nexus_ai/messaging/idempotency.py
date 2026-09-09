"""Durable outbound-send idempotency (NXS-P09, ADR-0073).

Scoped by ``(organization_id, account_id, idempotency_key)``. A SHA-256 fingerprint over
the canonical semantic request (account, conversation, sorted recipients, content,
subject, reply-to) is stored with the claim:

* SAME key + SAME fingerprint  -> the stored send result is replayed (``replayed=True``);
* SAME key + DIFFERENT fingerprint -> deterministic ``NXS_MSG_IDEMPOTENCY_CONFLICT``;
* a claim still ``PENDING`` -> retryable ``NXS_MSG_SEND_IN_PROGRESS``.

This is NOT exactly-once network delivery. A provider timeout is AMBIGUOUS: the claim is
finalised ``FAILED`` with ``NXS_MSG_TIMEOUT`` and a non-idempotent channel send is never
blindly re-attempted under the same key — the caller must decide with a new key.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from nexus_ai.messaging.entities import MessageContent, SendMessageRequest


class SendIdempotencyStatus(StrEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class SendIdempotencyRecord:
    idempotency_key: str
    request_fingerprint: str
    status: SendIdempotencyStatus
    message_id: UUID | None
    result_json: dict[str, Any] | None
    error_code: str | None
    updated_at: dt.datetime


@dataclass(frozen=True, slots=True)
class SendClaimOutcome:
    is_owner: bool
    existing: SendIdempotencyRecord | None = None


class SendIdempotencyStore(Protocol):
    async def claim(
        self,
        *,
        organization_id: UUID,
        account_id: UUID,
        idempotency_key: str,
        request_fingerprint: str,
        expires_at: dt.datetime,
    ) -> SendClaimOutcome: ...

    async def finalize(
        self,
        *,
        organization_id: UUID,
        account_id: UUID,
        idempotency_key: str,
        status: SendIdempotencyStatus,
        message_id: UUID | None,
        result_json: dict[str, Any] | None,
        error_code: str | None,
    ) -> None: ...

    async def get(
        self,
        *,
        organization_id: UUID,
        account_id: UUID,
        idempotency_key: str,
    ) -> SendIdempotencyRecord | None: ...


def _canonical_content(content: MessageContent) -> dict[str, Any]:
    return {
        "content_type": content.content_type.value,
        "text": content.text,
        "html": content.html,
        "media": [item.model_dump(mode="json") for item in content.media],
    }


def send_fingerprint(request: SendMessageRequest, normalized_recipients: list[str]) -> str:
    canonical = json.dumps(
        {
            "account_id": str(request.account_id),
            "conversation_id": str(request.conversation_id),
            "to": sorted(normalized_recipients),
            "content": _canonical_content(request.content),
            "subject": request.subject,
            "reply_to": (
                None if request.reply_to_message_id is None else str(request.reply_to_message_id)
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
