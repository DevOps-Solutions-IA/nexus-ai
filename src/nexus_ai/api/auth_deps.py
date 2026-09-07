"""Authenticated principal dependency (NXS-AUTH-004, NXS-AUTH-007).

Resolves the Bearer access token into a :class:`Principal`. The token verification is
cryptographic and stateless; session-state checks (revocation, user status) run on the
endpoints that need them via :class:`AuthService`. Missing or invalid credentials fail
closed with a stable 401.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from nexus_ai.core.errors import AuthenticationRequiredError, TokenValidationError
from nexus_ai.domain.auth.entities import Principal
from nexus_ai.domain.auth.tokens import TokenService

_BEARER_PREFIX = "Bearer "


def _bearer_token(request: Request) -> str:
    raw = request.headers.get("Authorization")
    if raw is None or not raw.strip():
        raise AuthenticationRequiredError("A bearer token is required.")
    if not raw.startswith(_BEARER_PREFIX):
        raise TokenValidationError("Invalid token.")
    return raw[len(_BEARER_PREFIX) :].strip()


def _token_service(request: Request) -> TokenService:
    from nexus_ai.api.dependencies import get_resources

    return get_resources(request).token_service


async def get_principal(request: Request) -> Principal:
    claims = _token_service(request).verify_access_token(_bearer_token(request))
    return Principal(
        user_id=claims.subject,
        session_id=claims.session_id,
        organization_id=claims.organization_id,
        token_id=claims.jti,
        issued_at=claims.issued_at,
        expires_at=claims.expires_at,
    )


PrincipalDep = Annotated[Principal, Depends(get_principal)]


async def get_live_principal(request: Request, principal: PrincipalDep) -> Principal:
    """Principal + canonical live-state validation (session not revoked/expired, user
    ACTIVE, membership ACTIVE, Organization operational). Platform control-plane
    operations use this: a stale or revoked token is blocked BEFORE business logic."""
    from nexus_ai.api.dependencies import get_resources

    resources = get_resources(request)
    await resources.principal_validator.require_valid(
        user_id=principal.user_id,
        session_id=principal.session_id,
        organization_id=principal.organization_id,
    )
    return principal


LivePrincipalDep = Annotated[Principal, Depends(get_live_principal)]
