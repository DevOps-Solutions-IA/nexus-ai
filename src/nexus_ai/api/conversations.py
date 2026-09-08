"""Conversation API surface (NXS-CUSTOMER-001).

Channel-neutral: P06 exposes open/read/close lifecycle operations only. There is no
message-sending endpoint and no provider logic — later channel phases plug into this
model.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request, status
from fastapi.responses import Response

from nexus_ai.api.authorization import (
    ConversationCloseDep,
    ConversationCreateDep,
    ConversationReadDep,
)
from nexus_ai.api.dependencies import TenantContextDep, get_resources
from nexus_ai.domain.customers.entities import ConversationView, CreateConversationRequest

conversations_router = APIRouter(prefix="/conversations", tags=["conversations"])


@conversations_router.post(
    "",
    response_model=ConversationView,
    summary="Open (or resolve) a Conversation",
    responses={
        401: {"description": "Invalid or revoked token"},
        403: {"description": "Missing conversation:create"},
        409: {
            "description": (
                "External thread key conflict, or the thread already resolves to a "
                "different Customer than the one requested"
            )
        },
    },
)
async def open_conversation(
    payload: CreateConversationRequest,
    context: TenantContextDep,
    _authorized: ConversationCreateDep,
    request: Request,
) -> Response:
    from fastapi.responses import JSONResponse

    conversation, created = await get_resources(request).conversations.open_or_resolve(
        context.organization_id, payload
    )
    return JSONResponse(
        content=conversation.public_view().model_dump(mode="json"),
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@conversations_router.get(
    "/{conversation_id}",
    response_model=ConversationView,
    summary="A Conversation in the caller's Organization",
    responses={
        401: {"description": "Invalid or revoked token"},
        403: {"description": "Missing conversation:read"},
        404: {"description": "Not found in this Organization"},
    },
)
async def read_conversation(
    conversation_id: UUID,
    context: TenantContextDep,
    _authorized: ConversationReadDep,
    request: Request,
) -> ConversationView:
    conversation = await get_resources(request).conversations.get(
        context.organization_id, conversation_id
    )
    return conversation.public_view()


@conversations_router.post(
    "/{conversation_id}/close",
    response_model=ConversationView,
    summary="Close a Conversation (idempotent)",
    responses={
        401: {"description": "Invalid or revoked token"},
        403: {"description": "Missing conversation:close"},
        409: {"description": "Invalid state transition"},
    },
)
async def close_conversation(
    conversation_id: UUID,
    context: TenantContextDep,
    _authorized: ConversationCloseDep,
    request: Request,
) -> ConversationView:
    conversation = await get_resources(request).conversations.close(
        context.organization_id, conversation_id
    )
    return conversation.public_view()
