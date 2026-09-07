"""Authentication API surface (NXS-AUTH-008).

Deliberately minimal: login, refresh, logout/revoke, current identity/session and the
caller's own memberships. No registration, no user-admin CRUD, no password material or
token material in responses or errors, generic authentication failures and stable NXS
problem-details codes.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from nexus_ai.api.auth_deps import PrincipalDep
from nexus_ai.api.dependencies import get_resources
from nexus_ai.domain.auth.entities import (
    AuthSession,
    LoginRequest,
    LogoutRequest,
    MembershipView,
    RefreshRequest,
    SessionView,
)

auth_router = APIRouter(prefix="/auth", tags=["auth"])


def _session_payload(session: AuthSession) -> dict[str, object]:
    return {
        "access_token": session.tokens.access_token,
        "refresh_token": session.tokens.refresh_token,
        "token_type": session.tokens.token_type,
        "expires_in": session.tokens.expires_in,
        "user_id": str(session.user_id),
        "organization_id": str(session.organization_id),
        "session_id": str(session.session_id),
    }


def _client_ip(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


@auth_router.post("/login", response_model=dict, summary="Authenticate and establish a session")
async def login(payload: LoginRequest, request: Request) -> dict[str, object]:
    session = await get_resources(request).auth.login(payload, client_ip=_client_ip(request))
    return _session_payload(session)


@auth_router.post(
    "/refresh",
    response_model=dict,
    summary="Rotate the refresh session and issue fresh tokens",
)
async def refresh(payload: RefreshRequest, request: Request) -> dict[str, object]:
    session = await get_resources(request).auth.refresh(payload.refresh_token)
    return _session_payload(session)


@auth_router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke the refresh session (idempotent)",
)
async def logout(payload: LogoutRequest, request: Request) -> Response:
    await get_resources(request).auth.logout(payload.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@auth_router.get("/me", response_model=SessionView, summary="Current identity and session")
async def me(principal: PrincipalDep, request: Request) -> SessionView:
    return await get_resources(request).auth.session_view(principal)


@auth_router.get(
    "/memberships",
    response_model=list[MembershipView],
    summary="The caller's own Organization memberships",
)
async def memberships(principal: PrincipalDep, request: Request) -> list[MembershipView]:
    return await get_resources(request).auth.list_memberships(principal)
