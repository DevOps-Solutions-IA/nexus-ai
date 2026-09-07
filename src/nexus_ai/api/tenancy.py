"""Tenant context resolution boundary (NXS-TENANT-002, NXS-AUTH-007).

``TenantContextResolver`` is the seam P03 completes. The authenticated resolver derives
``organization_id`` ONLY from a cryptographically verified access token whose ``org``
claim was minted server-side after membership verification — never from an
``X-Organization-ID`` header, a request body or any other caller-controlled input. The
header resolver exists only for local and test environments and configuration refuses
it in staging/production.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from starlette.requests import Request

from nexus_ai.core.config import TenancySettings
from nexus_ai.core.context import current_context
from nexus_ai.core.errors import TenantContextInvalidError, TokenValidationError
from nexus_ai.core.tenancy import TenantContext, TenantContextSource
from nexus_ai.domain.auth.tokens import TokenService

_AUTH_HEADER = "Authorization"
_BEARER_PREFIX = "Bearer "


class TenantContextResolver(Protocol):
    async def resolve(self, request: Request) -> TenantContext | None: ...


class NullTenantContextResolver:
    """Explicit no-trust resolver: no authenticated identity means no tenant scope."""

    async def resolve(self, request: Request) -> TenantContext | None:
        return None


class BearerTokenTenantContextResolver:
    """Authenticated production resolver (NXS-AUTH-007).

    No Authorization header → no scope (tenant endpoints answer 403 as in P02). An
    invalid or expired token fails closed with 401. A valid token's ``org`` claim is the
    only tenant authority; the token was minted by :class:`AuthService` strictly after
    server-side membership verification, so a forged, cross-tenant or stale scope cannot
    be constructed by the caller.
    """

    def __init__(self, tokens: TokenService) -> None:
        self._tokens = tokens

    async def resolve(self, request: Request) -> TenantContext | None:
        raw = request.headers.get(_AUTH_HEADER)
        if raw is None or not raw.strip():
            return None
        if not raw.startswith(_BEARER_PREFIX):
            raise TokenValidationError("Invalid token.")
        token = raw[len(_BEARER_PREFIX) :].strip()
        claims = self._tokens.verify_access_token(token)
        request_context = current_context()
        return TenantContext(
            organization_id=claims.organization_id,
            source=TenantContextSource.RESOLVED_IDENTITY,
            correlation_id=None if request_context is None else request_context.correlation_id,
        )


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


def build_resolver(settings: TenancySettings, tokens: TokenService) -> TenantContextResolver:
    if settings.header_resolver_enabled:
        return HeaderTenantContextResolver(settings.context_header)
    return BearerTokenTenantContextResolver(tokens)
