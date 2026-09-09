"""Messaging Channels API surface (NXS-P09).

Governed operations only: channel-account CRUD + credentials, one send endpoint, message
reads, and provider-specific inbound webhook routes. There is deliberately NO raw
provider-request endpoint, no arbitrary URL / method / header surface, no way to read a
provider access token, no raw SMTP command surface and no callback-forwarding.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status

from nexus_ai.api.auth_deps import PrincipalDep
from nexus_ai.api.authorization import (
    MessagingManageAccountsDep,
    MessagingReadDep,
    MessagingSendDep,
)
from nexus_ai.api.dependencies import TenantContextDep, get_resources
from nexus_ai.messaging.entities import (
    AccountStatus,
    CreateAccountRequest,
    MessageView,
    MessagingAccountView,
    SendMessageRequest,
    StoreAccountCredentialRequest,
    UpdateAccountRequest,
)
from nexus_ai.messaging.providers.base import WebhookContext

messaging_router = APIRouter(prefix="/messaging", tags=["messaging"])
messaging_webhooks_router = APIRouter(prefix="/webhooks/messaging", tags=["messaging"])

_Limit = Annotated[int, Query(ge=1, le=100)]


# --- accounts ------------------------------------------------------------------


@messaging_router.post(
    "/accounts", response_model=MessagingAccountView, status_code=status.HTTP_201_CREATED
)
async def create_account(
    payload: CreateAccountRequest,
    context: TenantContextDep,
    _authorized: MessagingManageAccountsDep,
    request: Request,
) -> MessagingAccountView:
    account = await get_resources(request).channels.create_account(context.organization_id, payload)
    return account.public_view()


@messaging_router.get("/accounts", response_model=list[MessagingAccountView])
async def list_accounts(
    context: TenantContextDep,
    _authorized: MessagingReadDep,
    request: Request,
    limit: _Limit = 50,
) -> list[MessagingAccountView]:
    rows = await get_resources(request).channels.list_accounts(context.organization_id, limit=limit)
    return [row.public_view() for row in rows]


@messaging_router.get("/accounts/{account_id}", response_model=MessagingAccountView)
async def get_account(
    account_id: UUID,
    context: TenantContextDep,
    _authorized: MessagingReadDep,
    request: Request,
) -> MessagingAccountView:
    account = await get_resources(request).channels.get_account(context.organization_id, account_id)
    return account.public_view()


@messaging_router.patch("/accounts/{account_id}", response_model=MessagingAccountView)
async def update_account(
    account_id: UUID,
    payload: UpdateAccountRequest,
    context: TenantContextDep,
    _authorized: MessagingManageAccountsDep,
    request: Request,
) -> MessagingAccountView:
    account = await get_resources(request).channels.update_account(
        context.organization_id, account_id, payload
    )
    return account.public_view()


@messaging_router.post("/accounts/{account_id}/enable", response_model=MessagingAccountView)
async def enable_account(
    account_id: UUID,
    context: TenantContextDep,
    _authorized: MessagingManageAccountsDep,
    request: Request,
) -> MessagingAccountView:
    account = await get_resources(request).channels.set_account_status(
        context.organization_id, account_id, AccountStatus.ACTIVE
    )
    return account.public_view()


@messaging_router.post("/accounts/{account_id}/disable", response_model=MessagingAccountView)
async def disable_account(
    account_id: UUID,
    context: TenantContextDep,
    _authorized: MessagingManageAccountsDep,
    request: Request,
) -> MessagingAccountView:
    account = await get_resources(request).channels.set_account_status(
        context.organization_id, account_id, AccountStatus.DISABLED
    )
    return account.public_view()


@messaging_router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: UUID,
    context: TenantContextDep,
    _authorized: MessagingManageAccountsDep,
    request: Request,
) -> Response:
    await get_resources(request).channels.delete_account(context.organization_id, account_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@messaging_router.post("/accounts/{account_id}/credentials", status_code=status.HTTP_204_NO_CONTENT)
async def store_account_credential(
    account_id: UUID,
    payload: StoreAccountCredentialRequest,
    context: TenantContextDep,
    _authorized: MessagingManageAccountsDep,
    request: Request,
) -> Response:
    await get_resources(request).channels.store_account_credential(
        context.organization_id, account_id, payload
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@messaging_router.delete(
    "/accounts/{account_id}/credentials", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_account_credential(
    account_id: UUID,
    context: TenantContextDep,
    _authorized: MessagingManageAccountsDep,
    request: Request,
) -> Response:
    await get_resources(request).channels.delete_account_credential(
        context.organization_id, account_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- messages ------------------------------------------------------------------


@messaging_router.post("/messages", response_model=MessageView, status_code=status.HTTP_201_CREATED)
async def send_message(
    payload: SendMessageRequest,
    principal: PrincipalDep,
    _authorized: MessagingSendDep,
    request: Request,
) -> MessageView:
    message = await get_resources(request).channels.send(
        principal.organization_id, principal.user_id, payload
    )
    return message.public_view()


@messaging_router.get("/messages/{message_id}", response_model=MessageView)
async def get_message(
    message_id: UUID,
    context: TenantContextDep,
    _authorized: MessagingReadDep,
    request: Request,
) -> MessageView:
    message = await get_resources(request).channels.get_message(context.organization_id, message_id)
    return message.public_view()


@messaging_router.get("/messages", response_model=list[MessageView])
async def list_messages(
    context: TenantContextDep,
    _authorized: MessagingReadDep,
    request: Request,
    conversation_id: UUID,
    limit: _Limit = 50,
    after_id: UUID | None = None,
) -> list[MessageView]:
    rows = await get_resources(request).channels.list_messages(
        context.organization_id, conversation_id, after_id=after_id, limit=limit
    )
    return [row.public_view() for row in rows]


# --- inbound webhooks (unauthenticated route; provider-signature authenticated) ---


async def _webhook(provider: str, token: str, request: Request) -> dict[str, Any]:
    body = await request.body()
    ctx = WebhookContext(
        method=request.method,
        headers={key: value for key, value in request.headers.items()},
        query={key: value for key, value in request.query_params.items()},
        body=body,
    )
    result = await get_resources(request).channel_webhooks.receive(provider, token, ctx)
    if result.challenge is not None:
        return {"challenge": result.challenge}
    return {
        "accepted": result.accepted,
        "received": result.received,
        "status_updates": result.status_updates,
        "replayed": result.replayed,
    }


@messaging_webhooks_router.get("/{provider}/{token}")
async def receive_webhook_challenge(provider: str, token: str, request: Request) -> Any:
    result = await _webhook(provider, token, request)
    # A provider challenge expects the raw challenge string echoed back.
    if "challenge" in result:
        return Response(content=str(result["challenge"]), media_type="text/plain")
    return result


@messaging_webhooks_router.post("/{provider}/{token}", status_code=status.HTTP_202_ACCEPTED)
async def receive_webhook(provider: str, token: str, request: Request) -> dict[str, Any]:
    return await _webhook(provider, token, request)
