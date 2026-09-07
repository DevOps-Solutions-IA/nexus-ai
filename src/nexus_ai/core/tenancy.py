"""Trusted tenant context and namespace primitives (NXS-TENANT-002, NXS-TENANT-005).

``TenantContext`` is immutable, resolved only from a trusted resolver boundary, and
propagated through :mod:`contextvars` so it never leaks between concurrent async tasks.
It is a plain serializable value so future cross-process work (P04 events, P18 cells)
can carry ``organization_id`` explicitly rather than relying on process-local state.

The canonical tenant identifier is ``organization_id`` (UUIDv7). There is no
``account_id`` / ``workspace_id`` / ``company_id`` — tenant ownership always resolves to
an Organization.
"""

from __future__ import annotations

import contextvars
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Self
from uuid import UUID

from nexus_ai.core.errors import TenantContextInvalidError, TenantContextRequiredError


class TenantContextSource(StrEnum):
    RESOLVED_IDENTITY = "resolved_identity"  # P03 authenticated identity (future)
    SYSTEM_BOOTSTRAP = "system_bootstrap"  # trusted internal / provisioning path
    TEST_HEADER = "test_header"  # local/test only


@dataclass(frozen=True, slots=True)
class TenantContext:
    organization_id: UUID
    source: TenantContextSource
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.organization_id, UUID):  # pragma: no cover - typing guard
            raise TenantContextInvalidError("organization_id must be a UUID")

    def as_log_fields(self) -> dict[str, str]:
        return {"organization_id": str(self.organization_id), "tenant_source": self.source.value}

    def to_portable(self) -> dict[str, str]:
        """Serializable form for future message/job propagation (never relies on ContextVar)."""
        payload = {"organization_id": str(self.organization_id), "source": self.source.value}
        if self.correlation_id is not None:
            payload["correlation_id"] = self.correlation_id
        return payload

    @classmethod
    def from_portable(cls, data: dict[str, str]) -> Self:
        try:
            organization_id = UUID(data["organization_id"])
            source = TenantContextSource(data["source"])
        except (KeyError, ValueError) as exc:
            raise TenantContextInvalidError("malformed portable tenant context") from exc
        return cls(organization_id, source, data.get("correlation_id"))


_current: contextvars.ContextVar[TenantContext | None] = contextvars.ContextVar(
    "nexus_ai_tenant_context", default=None
)


def current_tenant() -> TenantContext | None:
    return _current.get()


def require_tenant() -> TenantContext:
    context = _current.get()
    if context is None:
        raise TenantContextRequiredError("This operation requires a trusted tenant context.")
    return context


@contextmanager
def tenant_scope(context: TenantContext) -> Iterator[TenantContext]:
    """Bind a trusted tenant context for the duration of the block (per-task isolated).

    Use this when enter and exit run in the same frame (tests, workers). On a FastAPI
    request path use :func:`bind_tenant` instead — a ``ContextVar`` token cannot be reset
    across the async-exit-stack boundary of a yield-dependency.
    """
    token = _current.set(context)
    try:
        yield context
    finally:
        _current.reset(token)


def bind_tenant(context: TenantContext) -> None:
    """Bind the tenant context for the current task without a resettable token.

    Safe on the request path: Starlette runs each request in its own copied context, so
    the binding is discarded when the request task ends and never leaks to other requests.
    """
    _current.set(context)


def tenant_log_fields() -> dict[str, str]:
    context = _current.get()
    return {} if context is None else context.as_log_fields()


# --- Tenant resource namespace (NXS-TENANT-005) -----------------------------------------------

_NAMESPACE_PREFIX = "nxs"
_SEGMENT = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def tenant_namespace(organization_id: UUID, *segments: str) -> str:
    """Deterministic ``nxs:<organization_id>:<segment>:...`` key with no global fallback.

    Used later for Valkey keys, distributed locks, object-storage prefixes and job
    identity. Every tenant-scoped resource name MUST be built through this function.
    """
    if not isinstance(organization_id, UUID):
        raise TenantContextInvalidError("tenant_namespace requires a UUID organization_id")
    if not segments:
        raise TenantContextInvalidError("tenant_namespace requires at least one resource segment")
    cleaned: list[str] = []
    for segment in segments:
        candidate = segment.strip()
        if not _SEGMENT.match(candidate):
            raise TenantContextInvalidError(f"unsafe tenant namespace segment: {segment!r}")
        cleaned.append(candidate)
    return ":".join([_NAMESPACE_PREFIX, str(organization_id), *cleaned])
