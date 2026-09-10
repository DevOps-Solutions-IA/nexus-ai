"""ElevenLabs Voice API surface (NXS-P12).

Governed operations only: provider-account + voice-profile CRUD, start / get / list /
stop a voice session attached to an ACTIVE NXS-P11 media session, request an AI->human
handoff, and the signed provider callback route. There is deliberately NO raw provider
endpoint, WebSocket URL, API key, arbitrary JSON pass-through, ARI command, media
destination or shell surface.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status

from nexus_ai.api.authorization import VoiceConfigureDep, VoiceReadDep, VoiceUseDep
from nexus_ai.api.dependencies import TenantContextDep, get_resources
from nexus_ai.voice.entities import (
    CreateVoiceAccountRequest,
    CreateVoiceProfileRequest,
    RequestHandoffRequest,
    StartVoiceSessionRequest,
    StopVoiceSessionRequest,
    StoreVoiceCredentialRequest,
    UpdateVoiceAccountRequest,
    UpdateVoiceProfileRequest,
    VoiceAccountStatus,
    VoiceProfileStatus,
    VoiceProfileView,
    VoiceProviderAccountView,
    VoiceSessionView,
)
from nexus_ai.voice.providers.base import WebhookContext

voice_router = APIRouter(prefix="/voice", tags=["voice"])
voice_webhooks_router = APIRouter(prefix="/webhooks/voice", tags=["voice"])

_Limit = Annotated[int, Query(ge=1, le=100)]


# --- accounts ----------------------------------------------------------------


@voice_router.post(
    "/accounts", response_model=VoiceProviderAccountView, status_code=status.HTTP_201_CREATED
)
async def create_account(
    payload: CreateVoiceAccountRequest,
    context: TenantContextDep,
    _authorized: VoiceConfigureDep,
    request: Request,
) -> VoiceProviderAccountView:
    account = await get_resources(request).voice.create_account(context.organization_id, payload)
    return account.public_view()


@voice_router.get("/accounts", response_model=list[VoiceProviderAccountView])
async def list_accounts(
    context: TenantContextDep,
    _authorized: VoiceReadDep,
    request: Request,
    limit: _Limit = 50,
) -> list[VoiceProviderAccountView]:
    rows = await get_resources(request).voice.list_accounts(context.organization_id, limit=limit)
    return [row.public_view() for row in rows]


@voice_router.get("/accounts/{account_id}", response_model=VoiceProviderAccountView)
async def get_account(
    account_id: UUID,
    context: TenantContextDep,
    _authorized: VoiceReadDep,
    request: Request,
) -> VoiceProviderAccountView:
    account = await get_resources(request).voice.get_account(context.organization_id, account_id)
    return account.public_view()


@voice_router.patch("/accounts/{account_id}", response_model=VoiceProviderAccountView)
async def update_account(
    account_id: UUID,
    payload: UpdateVoiceAccountRequest,
    context: TenantContextDep,
    _authorized: VoiceConfigureDep,
    request: Request,
) -> VoiceProviderAccountView:
    account = await get_resources(request).voice.update_account(
        context.organization_id, account_id, payload.configuration or {}
    )
    return account.public_view()


@voice_router.post("/accounts/{account_id}/disable", response_model=VoiceProviderAccountView)
async def disable_account(
    account_id: UUID,
    context: TenantContextDep,
    _authorized: VoiceConfigureDep,
    request: Request,
) -> VoiceProviderAccountView:
    account = await get_resources(request).voice.set_account_status(
        context.organization_id, account_id, VoiceAccountStatus.DISABLED
    )
    return account.public_view()


@voice_router.post("/accounts/{account_id}/enable", response_model=VoiceProviderAccountView)
async def enable_account(
    account_id: UUID,
    context: TenantContextDep,
    _authorized: VoiceConfigureDep,
    request: Request,
) -> VoiceProviderAccountView:
    account = await get_resources(request).voice.set_account_status(
        context.organization_id, account_id, VoiceAccountStatus.ACTIVE
    )
    return account.public_view()


@voice_router.post("/accounts/{account_id}/credentials", status_code=status.HTTP_204_NO_CONTENT)
async def store_account_credential(
    account_id: UUID,
    payload: StoreVoiceCredentialRequest,
    context: TenantContextDep,
    _authorized: VoiceConfigureDep,
    request: Request,
) -> Response:
    await get_resources(request).voice.store_account_credential(
        context.organization_id, account_id, dict(payload.fields)
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@voice_router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: UUID,
    context: TenantContextDep,
    _authorized: VoiceConfigureDep,
    request: Request,
) -> Response:
    await get_resources(request).voice.delete_account(context.organization_id, account_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- profiles --------------------------------------------------------------


@voice_router.post(
    "/profiles", response_model=VoiceProfileView, status_code=status.HTTP_201_CREATED
)
async def create_profile(
    payload: CreateVoiceProfileRequest,
    context: TenantContextDep,
    _authorized: VoiceConfigureDep,
    request: Request,
) -> VoiceProfileView:
    profile = await get_resources(request).voice.create_profile(context.organization_id, payload)
    return profile.public_view()


@voice_router.get("/profiles", response_model=list[VoiceProfileView])
async def list_profiles(
    context: TenantContextDep,
    _authorized: VoiceReadDep,
    request: Request,
    limit: _Limit = 50,
) -> list[VoiceProfileView]:
    rows = await get_resources(request).voice.list_profiles(context.organization_id, limit=limit)
    return [row.public_view() for row in rows]


@voice_router.get("/profiles/{profile_id}", response_model=VoiceProfileView)
async def get_profile(
    profile_id: UUID,
    context: TenantContextDep,
    _authorized: VoiceReadDep,
    request: Request,
) -> VoiceProfileView:
    profile = await get_resources(request).voice.get_profile(context.organization_id, profile_id)
    return profile.public_view()


@voice_router.patch("/profiles/{profile_id}", response_model=VoiceProfileView)
async def update_profile(
    profile_id: UUID,
    payload: UpdateVoiceProfileRequest,
    context: TenantContextDep,
    _authorized: VoiceConfigureDep,
    request: Request,
) -> VoiceProfileView:
    profile = await get_resources(request).voice.update_profile(
        context.organization_id, profile_id, payload
    )
    return profile.public_view()


@voice_router.post("/profiles/{profile_id}/disable", response_model=VoiceProfileView)
async def disable_profile(
    profile_id: UUID,
    context: TenantContextDep,
    _authorized: VoiceConfigureDep,
    request: Request,
) -> VoiceProfileView:
    profile = await get_resources(request).voice.set_profile_status(
        context.organization_id, profile_id, VoiceProfileStatus.DISABLED
    )
    return profile.public_view()


# --- sessions ------------------------------------------------------------


@voice_router.post(
    "/sessions", response_model=VoiceSessionView, status_code=status.HTTP_201_CREATED
)
async def start_session(
    payload: StartVoiceSessionRequest,
    context: TenantContextDep,
    _authorized: VoiceUseDep,
    request: Request,
) -> VoiceSessionView:
    session = await get_resources(request).voice.start_session(context.organization_id, payload)
    return session.public_view()


@voice_router.get("/sessions", response_model=list[VoiceSessionView])
async def list_sessions(
    context: TenantContextDep,
    _authorized: VoiceReadDep,
    request: Request,
    account_id: UUID | None = None,
    call_id: UUID | None = None,
    limit: _Limit = 50,
) -> list[VoiceSessionView]:
    rows = await get_resources(request).voice.list_sessions(
        context.organization_id, account_id=account_id, call_id=call_id, limit=limit
    )
    return [row.public_view() for row in rows]


@voice_router.get("/sessions/{session_id}", response_model=VoiceSessionView)
async def get_session(
    session_id: UUID,
    context: TenantContextDep,
    _authorized: VoiceReadDep,
    request: Request,
) -> VoiceSessionView:
    session = await get_resources(request).voice.get_session(context.organization_id, session_id)
    return session.public_view()


@voice_router.post("/sessions/{session_id}/stop", response_model=VoiceSessionView)
async def stop_session(
    session_id: UUID,
    payload: StopVoiceSessionRequest,
    context: TenantContextDep,
    _authorized: VoiceUseDep,
    request: Request,
) -> VoiceSessionView:
    session = await get_resources(request).voice.stop_session(
        context.organization_id, session_id, payload
    )
    return session.public_view()


@voice_router.post("/sessions/{session_id}/handoff", response_model=VoiceSessionView)
async def request_handoff(
    session_id: UUID,
    payload: RequestHandoffRequest,
    context: TenantContextDep,
    _authorized: VoiceUseDep,
    request: Request,
) -> VoiceSessionView:
    session = await get_resources(request).voice.request_handoff(
        context.organization_id, session_id, payload
    )
    return session.public_view()


# --- inbound webhooks --------------------------------------------------------


def _webhook_context(request: Request, body: bytes) -> WebhookContext:
    return WebhookContext(
        method=request.method,
        headers={k: v for k, v in request.headers.items()},
        query={k: v for k, v in request.query_params.items()},
        body=body,
    )


@voice_webhooks_router.post("/{provider}/{token}", status_code=status.HTTP_202_ACCEPTED)
async def receive_webhook(provider: str, token: str, request: Request) -> dict[str, Any]:
    body = await request.body()
    ctx = _webhook_context(request, body)
    result = await get_resources(request).voice_webhooks.receive(provider, token, ctx)
    return {"accepted": result.accepted, "processed": result.processed}
