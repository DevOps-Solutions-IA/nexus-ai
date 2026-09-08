"""Customer API surface (NXS-CUSTOMER-001).

Tenant-safe, RBAC-enforced, bounded. Identity lookups operate ONLY on normalized
values inside the caller's Organization scope; there is deliberately no global lookup
by phone/email and no arbitrary-id cross-tenant read.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status

from nexus_ai.api.authorization import (
    ConversationReadDep,
    CustomerCreateDep,
    CustomerIdentityLinkDep,
    CustomerReadDep,
    TimelineReadDep,
)
from nexus_ai.api.dependencies import TenantContextDep, get_resources
from nexus_ai.domain.customers.entities import (
    ConversationView,
    CreateCustomerRequest,
    CustomerIdentityView,
    CustomerView,
    LinkIdentityRequest,
    TimelineActivityView,
)

customers_router = APIRouter(prefix="/customers", tags=["customers"])

_Limit = Annotated[int, Query(ge=1, le=100)]


@customers_router.post(
    "",
    response_model=CustomerView,
    summary="Create (or resolve) a Customer by canonical identity",
    responses={
        401: {"description": "Invalid or revoked token"},
        403: {"description": "Missing customer:create"},
        422: {"description": "Invalid identity or payload"},
    },
)
async def create_customer(
    payload: CreateCustomerRequest,
    context: TenantContextDep,
    _authorized: CustomerCreateDep,
    request: Request,
) -> Response:
    from fastapi.responses import JSONResponse

    customer, created = await get_resources(request).customers.resolve_or_create(
        context.organization_id, payload
    )
    return JSONResponse(
        content=customer.public_view().model_dump(mode="json"),
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@customers_router.get(
    "/{customer_id}",
    response_model=CustomerView,
    summary="A Customer in the caller's Organization",
    responses={
        401: {"description": "Invalid or revoked token"},
        403: {"description": "Missing customer:read"},
        404: {"description": "Not found in this Organization"},
    },
)
async def read_customer(
    customer_id: UUID,
    context: TenantContextDep,
    _authorized: CustomerReadDep,
    request: Request,
) -> CustomerView:
    customer = await get_resources(request).customers.get(context.organization_id, customer_id)
    return customer.public_view()


@customers_router.get(
    "",
    response_model=list[CustomerView],
    summary="Resolve a Customer by canonical identity (tenant-scoped)",
    responses={
        401: {"description": "Invalid or revoked token"},
        403: {"description": "Missing customer:read"},
        422: {"description": "Invalid identity"},
    },
)
async def resolve_customer(
    context: TenantContextDep,
    _authorized: CustomerReadDep,
    request: Request,
    identity_type: str = Query(min_length=1, max_length=16),
    identity: str = Query(min_length=1, max_length=320),
) -> list[CustomerView]:
    from nexus_ai.domain.customers.entities import IdentityType
    from nexus_ai.domain.customers.normalization import normalize_identity_value

    try:
        resolved_type = IdentityType(identity_type)
    except ValueError:
        from nexus_ai.core.errors import UnsupportedIdentityTypeError

        raise UnsupportedIdentityTypeError(
            "this identity type is not supported", extensions={"identity_type": identity_type}
        ) from None
    normalized = normalize_identity_value(resolved_type, identity)
    resources = get_resources(request)
    async with resources.database.tenant_transaction(context.organization_id) as tenant:
        from nexus_ai.domain.customers.repository import (
            CustomerIdentityRepository,
        )

        match = await CustomerIdentityRepository(tenant).resolve(resolved_type, normalized)
    if match is None:
        return []
    customer = await resources.customers.get(context.organization_id, match.customer_id)
    return [customer.public_view()]


@customers_router.get(
    "/{customer_id}/identities",
    response_model=list[CustomerIdentityView],
    summary="A Customer's channel identities",
    responses={
        401: {"description": "Invalid or revoked token"},
        403: {"description": "Missing customer:read"},
        404: {"description": "Not found in this Organization"},
    },
)
async def list_customer_identities(
    customer_id: UUID,
    context: TenantContextDep,
    _authorized: CustomerReadDep,
    request: Request,
) -> list[CustomerIdentityView]:
    identities = await get_resources(request).customers.list_identities(
        context.organization_id, customer_id
    )
    return [identity.public_view() for identity in identities]


@customers_router.post(
    "/{customer_id}/identities",
    response_model=CustomerIdentityView,
    status_code=status.HTTP_201_CREATED,
    summary="Link a new channel identity to a Customer",
    responses={
        401: {"description": "Invalid or revoked token"},
        403: {"description": "Missing customer:identity:link"},
        409: {"description": "Identity already belongs to another Customer"},
        422: {"description": "Invalid identity"},
    },
)
async def link_customer_identity(
    customer_id: UUID,
    payload: LinkIdentityRequest,
    context: TenantContextDep,
    _authorized: CustomerIdentityLinkDep,
    request: Request,
) -> CustomerIdentityView:
    identity = await get_resources(request).customers.link_identity(
        context.organization_id, customer_id, payload
    )
    return identity.public_view()


@customers_router.get(
    "/{customer_id}/conversations",
    response_model=list[ConversationView],
    summary="A Customer's conversations (newest first, cursor-paginated)",
    responses={
        401: {"description": "Invalid or revoked token"},
        403: {"description": "Missing conversation:read"},
        404: {"description": "Not found in this Organization"},
    },
)
async def list_customer_conversations(
    customer_id: UUID,
    context: TenantContextDep,
    _authorized: ConversationReadDep,
    request: Request,
    limit: _Limit = 20,
    after_id: UUID | None = None,
) -> list[ConversationView]:
    conversations = await get_resources(request).conversations.list_for_customer(
        context.organization_id, customer_id, after_id=after_id, limit=limit
    )
    return [conversation.public_view() for conversation in conversations[:limit]]


@customers_router.get(
    "/{customer_id}/timeline",
    response_model=list[TimelineActivityView],
    summary="The unified customer timeline (deterministic order, cursor-paginated)",
    responses={
        401: {"description": "Invalid or revoked token"},
        403: {"description": "Missing customer:timeline:read"},
        404: {"description": "Not found in this Organization"},
    },
)
async def read_customer_timeline(
    customer_id: UUID,
    context: TenantContextDep,
    _authorized: TimelineReadDep,
    request: Request,
    limit: _Limit = 20,
    after_occurred_at: dt.datetime | None = None,
    after_id: UUID | None = None,
) -> list[TimelineActivityView]:
    after = None
    if after_occurred_at is not None and after_id is not None:
        after = (after_occurred_at, after_id)
    return await get_resources(request).customers.timeline(
        context.organization_id, customer_id, after=after, limit=limit
    )
