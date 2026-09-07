"""Reusable typed API contract primitives (NXS-API-001).

Pagination, an ``Idempotency-Key`` request contract and forward-safe request metadata.
Persistent idempotency storage depends on P04 and is intentionally not implemented; the
HTTP contract is established now. No tenancy or auth is implied by these types.
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

_IDEMPOTENCY_KEY = re.compile(r"\A[A-Za-z0-9_.:-]{8,200}\Z")


class PageParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    limit: Annotated[int, Field(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE
    cursor: Annotated[str | None, Field(max_length=512)] = None


class Page[T](BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[T]
    next_cursor: str | None = None
    limit: int


def parse_idempotency_key(raw: str | None) -> str | None:
    """Validate an ``Idempotency-Key`` header value; raise for a malformed key."""
    if raw is None:
        return None
    candidate = raw.strip()
    if not _IDEMPOTENCY_KEY.match(candidate):
        from nexus_ai.core.errors import InvalidRequestError

        raise InvalidRequestError(
            "The Idempotency-Key header must be 8-200 characters of [A-Za-z0-9_.:-]."
        )
    return candidate


class RequestMetadata(BaseModel):
    """Forward-safe per-request metadata. Domain identity fields are added by later phases."""

    model_config = ConfigDict(frozen=True)

    request_id: str
    correlation_id: str
    trace_id: str | None = None
    idempotency_key: str | None = None
