"""Telephony Foundation API surface (NXS-P11).

Governed operations only: account + phone-number CRUD, one outbound call endpoint,
hangup, DTMF, call reads, and provider-specific inbound webhook routes. There is
deliberately NO raw SIP header, dialplan, ARI / AMI action, provider-credential or
shell surface, and no ``from`` free-string (caller ID is an owned ``from_number_id``).
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status

from nexus_ai.api.authorization import (
    TelephonyCallDep,
    TelephonyConfigureDep,
    TelephonyHangupDep,
    TelephonyReadDep,
)
from nexus_ai.api.dependencies import TenantContextDep, get_resources
from nexus_ai.telephony.entities import (
    AccountStatus,
    CallView,
    CreateAccountRequest,
    CreateCallRequest,
    DtmfResult,
    HangupCallRequest,
    PhoneNumberView,
    RegisterPhoneNumberRequest,
    SendDtmfRequest,
    StoreAccountCredentialRequest,
    TelephonyAccountView,
    UpdateAccountRequest,
)
from nexus_ai.telephony.providers.base import WebhookContext

telephony_router = APIRouter(prefix="/telephony", tags=["telephony"])
telephony_webhooks_router = APIRouter(prefix="/webhooks/telephony", tags=["telephony"])

_Limit = Annotated[int, Query(ge=1, le=100)]


# --- accounts ------------------------------------------------------------------


@telephony_router.post(
    "/accounts", response_model=TelephonyAccountView, status_code=status.HTTP_201_CREATED
)
async def create_account(
    payload: CreateAccountRequest,
    context: TenantContextDep,
    _authorized: TelephonyConfigureDep,
    request: Request,
) -> TelephonyAccountView:
    account = await get_resources(request).telephony.create_account(
        context.organization_id, payload
    )
    return account.public_view()


@telephony_router.get("/accounts", response_model=list[TelephonyAccountView])
async def list_accounts(
    context: TenantContextDep,
    _authorized: TelephonyReadDep,
    request: Request,
    limit: _Limit = 50,
) -> list[TelephonyAccountView]:
    rows = await get_resources(request).telephony.list_accounts(
        context.organization_id, limit=limit
    )
    return [row.public_view() for row in rows]


@telephony_router.get("/accounts/{account_id}", response_model=TelephonyAccountView)
async def get_account(
    account_id: UUID,
    context: TenantContextDep,
    _authorized: TelephonyReadDep,
    request: Request,
) -> TelephonyAccountView:
    account = await get_resources(request).telephony.get_account(
        context.organization_id, account_id
    )
    return account.public_view()


@telephony_router.patch("/accounts/{account_id}", response_model=TelephonyAccountView)
async def update_account(
    account_id: UUID,
    payload: UpdateAccountRequest,
    context: TenantContextDep,
    _authorized: TelephonyConfigureDep,
    request: Request,
) -> TelephonyAccountView:
    account = await get_resources(request).telephony.update_account(
        context.organization_id, account_id, payload.configuration or {}
    )
    return account.public_view()


@telephony_router.post("/accounts/{account_id}/disable", response_model=TelephonyAccountView)
async def disable_account(
    account_id: UUID,
    context: TenantContextDep,
    _authorized: TelephonyConfigureDep,
    request: Request,
) -> TelephonyAccountView:
    account = await get_resources(request).telephony.set_account_status(
        context.organization_id, account_id, AccountStatus.DISABLED
    )
    return account.public_view()


@telephony_router.post("/accounts/{account_id}/enable", response_model=TelephonyAccountView)
async def enable_account(
    account_id: UUID,
    context: TenantContextDep,
    _authorized: TelephonyConfigureDep,
    request: Request,
) -> TelephonyAccountView:
    account = await get_resources(request).telephony.set_account_status(
        context.organization_id, account_id, AccountStatus.ACTIVE
    )
    return account.public_view()


@telephony_router.post("/accounts/{account_id}/credentials", status_code=status.HTTP_204_NO_CONTENT)
async def store_account_credential(
    account_id: UUID,
    payload: StoreAccountCredentialRequest,
    context: TenantContextDep,
    _authorized: TelephonyConfigureDep,
    request: Request,
) -> Response:
    await get_resources(request).telephony.store_account_credential(
        context.organization_id, account_id, dict(payload.fields)
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@telephony_router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: UUID,
    context: TenantContextDep,
    _authorized: TelephonyConfigureDep,
    request: Request,
) -> Response:
    await get_resources(request).telephony.delete_account(context.organization_id, account_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- phone numbers ------------------------------------------------------------


@telephony_router.post(
    "/numbers", response_model=PhoneNumberView, status_code=status.HTTP_201_CREATED
)
async def register_number(
    payload: RegisterPhoneNumberRequest,
    context: TenantContextDep,
    _authorized: TelephonyConfigureDep,
    request: Request,
) -> PhoneNumberView:
    number = await get_resources(request).telephony.register_number(
        context.organization_id, payload
    )
    return number.public_view()


@telephony_router.get("/numbers", response_model=list[PhoneNumberView])
async def list_numbers(
    context: TenantContextDep,
    _authorized: TelephonyReadDep,
    request: Request,
    limit: _Limit = 50,
) -> list[PhoneNumberView]:
    rows = await get_resources(request).telephony.list_numbers(context.organization_id, limit=limit)
    return [row.public_view() for row in rows]


@telephony_router.get("/numbers/{number_id}", response_model=PhoneNumberView)
async def get_number(
    number_id: UUID,
    context: TenantContextDep,
    _authorized: TelephonyReadDep,
    request: Request,
) -> PhoneNumberView:
    number = await get_resources(request).telephony.get_number(context.organization_id, number_id)
    return number.public_view()


@telephony_router.post("/numbers/{number_id}/verify", response_model=PhoneNumberView)
async def verify_number(
    number_id: UUID,
    context: TenantContextDep,
    _authorized: TelephonyConfigureDep,
    request: Request,
) -> PhoneNumberView:
    number = await get_resources(request).telephony.set_number_verified(
        context.organization_id, number_id, verified=True
    )
    return number.public_view()


# --- calls -------------------------------------------------------------------


@telephony_router.post("/calls", response_model=CallView, status_code=status.HTTP_201_CREATED)
async def create_call(
    payload: CreateCallRequest,
    context: TenantContextDep,
    _authorized: TelephonyCallDep,
    request: Request,
) -> CallView:
    call = await get_resources(request).telephony.create_call(
        context.organization_id, None, payload
    )
    return call.public_view()


@telephony_router.get("/calls", response_model=list[CallView])
async def list_calls(
    context: TenantContextDep,
    _authorized: TelephonyReadDep,
    request: Request,
    account_id: UUID | None = None,
    limit: _Limit = 50,
) -> list[CallView]:
    rows = await get_resources(request).telephony.list_calls(
        context.organization_id, account_id=account_id, limit=limit
    )
    return [row.public_view() for row in rows]


@telephony_router.get("/calls/{call_id}", response_model=CallView)
async def get_call(
    call_id: UUID,
    context: TenantContextDep,
    _authorized: TelephonyReadDep,
    request: Request,
) -> CallView:
    call = await get_resources(request).telephony.get_call(context.organization_id, call_id)
    return call.public_view()


@telephony_router.post("/calls/{call_id}/hangup", response_model=CallView)
async def hangup_call(
    call_id: UUID,
    payload: HangupCallRequest,
    context: TenantContextDep,
    _authorized: TelephonyHangupDep,
    request: Request,
) -> CallView:
    call = await get_resources(request).telephony.hangup_call(
        context.organization_id, call_id, payload
    )
    return call.public_view()


@telephony_router.post("/calls/{call_id}/dtmf", response_model=DtmfResult)
async def send_dtmf(
    call_id: UUID,
    payload: SendDtmfRequest,
    context: TenantContextDep,
    _authorized: TelephonyCallDep,
    request: Request,
) -> DtmfResult:
    return await get_resources(request).telephony.send_dtmf(
        context.organization_id, call_id, payload
    )


# --- inbound webhooks -------------------------------------------------------


def _webhook_context(request: Request, body: bytes) -> WebhookContext:
    return WebhookContext(
        method=request.method,
        headers={k: v for k, v in request.headers.items()},
        query={k: v for k, v in request.query_params.items()},
        body=body,
    )


@telephony_webhooks_router.get("/{provider}/{token}")
async def receive_webhook_challenge(provider: str, token: str, request: Request) -> Any:
    ctx = _webhook_context(request, b"")
    answer = await get_resources(request).telephony_webhooks.challenge(provider, token, ctx)
    return Response(content=answer or "", media_type="text/plain")


@telephony_webhooks_router.post("/{provider}/{token}", status_code=status.HTTP_202_ACCEPTED)
async def receive_webhook(provider: str, token: str, request: Request) -> dict[str, Any]:
    body = await request.body()
    ctx = _webhook_context(request, body)
    result = await get_resources(request).telephony_webhooks.receive(provider, token, ctx)
    return {"accepted": result.accepted, "processed": result.processed}
