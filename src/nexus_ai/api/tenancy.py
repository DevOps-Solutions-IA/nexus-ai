"""Tenant context resolution boundary (NXS-TENANT-002, section 34/35).

``TenantContextResolver`` is the seam P03 will implement with authenticated identity and
RBAC claims. Until then the production default resolves NO trusted context: a caller
cannot become an Organization by asserting a header. The header resolver exists only for
local and test environments and configuration refuses it in staging/production.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from starlette.requests import Request

from nexus_ai.core.config import TenancySettings
from nexus_ai.core.context import current_context
from nexus_ai.core.errors import TenantContextInvalidError
from nexus_ai.core.tenancy import TenantContext, TenantContextSource


class TenantContextResolver(Protocol):
    async def resolve(self, request: Request) -> TenantContext | None: ...


class NullTenantContextResolver:
    """Production/staging default: no authenticated identity means no trusted tenant scope."""

    async def resolve(self, request: Request) -> TenantContext | None:
        return None


class HeaderTenantContextResolver:
    """LOCAL / TEST ONLY. Trusts a configured header to carry organization_id."""

    source = TenantContextSource.TEST_HEADER

    def __init__(self, header: str) -> None:
        self._header = header

    async def resolve(self, request: Request) -> TenantContext | None:
        raw = request.headers.get(self._header)
        if raw is None or not raw.strip():
            return None
        try:
            organization_id = UUID(raw.strip())
        except ValueError as exc:
            raise TenantContextInvalidError("Tenant header is not a valid UUID.") from exc
        request_context = current_context()
        return TenantContext(
            organization_id=organization_id,
            source=self.source,
            correlation_id=None if request_context is None else request_context.correlation_id,
        )


def build_resolver(settings: TenancySettings) -> TenantContextResolver:
    if settings.header_resolver_enabled:
        return HeaderTenantContextResolver(settings.context_header)
    return NullTenantContextResolver()
