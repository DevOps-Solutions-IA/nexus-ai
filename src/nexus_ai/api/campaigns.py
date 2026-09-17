"""Tenant-scoped Campaign management and execution-control API (NXS-P16)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, status

from nexus_ai.api.authorization import CampaignConfigureDep, CampaignExecuteDep, CampaignReadDep
from nexus_ai.api.dependencies import RequestMetadataDep, TenantContextDep, get_resources
from nexus_ai.campaigns.entities import (
    Campaign,
    CampaignRecipient,
    CampaignRun,
    CampaignTransition,
    ContactPreferenceRequest,
    CreateCampaignRequest,
    ScheduleCampaignRequest,
    SuppressionRequest,
    UpdateCampaignRequest,
)
from nexus_ai.campaigns.service import CampaignService

campaigns_router = APIRouter(tags=["campaigns"])
_Limit = Annotated[int, Query(ge=1, le=100)]
_Offset = Annotated[int, Query(ge=0, le=1_000_000)]


def _service(request: Request) -> CampaignService:
    return get_resources(request).campaigns


@campaigns_router.post("/campaigns", response_model=Campaign, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    payload: CreateCampaignRequest,
    context: TenantContextDep,
    _authorized: CampaignConfigureDep,
    request: Request,
) -> Campaign:
    return await _service(request).create_campaign(context.organization_id, payload)


@campaigns_router.get("/campaigns", response_model=list[Campaign])
async def list_campaigns(
    context: TenantContextDep,
    _authorized: CampaignReadDep,
    request: Request,
    limit: _Limit = 50,
    offset: _Offset = 0,
) -> list[Campaign]:
    return await _service(request).list_campaigns(
        context.organization_id, limit=limit, offset=offset
    )


@campaigns_router.get("/campaigns/{campaign_id}", response_model=Campaign)
async def get_campaign(
    campaign_id: UUID,
    context: TenantContextDep,
    _authorized: CampaignReadDep,
    request: Request,
) -> Campaign:
    return await _service(request).get_campaign(context.organization_id, campaign_id)


@campaigns_router.patch("/campaigns/{campaign_id}", response_model=Campaign)
async def update_campaign(
    campaign_id: UUID,
    payload: UpdateCampaignRequest,
    context: TenantContextDep,
    _authorized: CampaignConfigureDep,
    request: Request,
) -> Campaign:
    return await _service(request).update_campaign(context.organization_id, campaign_id, payload)


@campaigns_router.post("/campaigns/{campaign_id}/prepare", response_model=Campaign)
async def prepare_campaign(
    campaign_id: UUID,
    context: TenantContextDep,
    _authorized: CampaignExecuteDep,
    request: Request,
) -> Campaign:
    return await _service(request).prepare_campaign(context.organization_id, campaign_id)


@campaigns_router.post("/campaigns/{campaign_id}/schedule", response_model=Campaign)
async def schedule_campaign(
    campaign_id: UUID,
    payload: ScheduleCampaignRequest,
    context: TenantContextDep,
    _authorized: CampaignExecuteDep,
    request: Request,
) -> Campaign:
    return await _service(request).schedule_campaign(context.organization_id, campaign_id, payload)


@campaigns_router.post("/campaigns/{campaign_id}/start", response_model=CampaignRun)
async def start_campaign(
    campaign_id: UUID,
    context: TenantContextDep,
    metadata: RequestMetadataDep,
    _authorized: CampaignExecuteDep,
    request: Request,
) -> CampaignRun:
    return await _service(request).start_campaign(
        context.organization_id, campaign_id, idempotency_key=metadata.idempotency_key
    )


@campaigns_router.post("/campaigns/{campaign_id}/pause", response_model=Campaign)
async def pause_campaign(
    campaign_id: UUID,
    context: TenantContextDep,
    _authorized: CampaignExecuteDep,
    request: Request,
) -> Campaign:
    return await _service(request).pause_campaign(context.organization_id, campaign_id)


@campaigns_router.post("/campaigns/{campaign_id}/resume", response_model=Campaign)
async def resume_campaign(
    campaign_id: UUID,
    context: TenantContextDep,
    _authorized: CampaignExecuteDep,
    request: Request,
) -> Campaign:
    return await _service(request).resume_campaign(context.organization_id, campaign_id)


@campaigns_router.post("/campaigns/{campaign_id}/cancel", response_model=Campaign)
async def cancel_campaign(
    campaign_id: UUID,
    context: TenantContextDep,
    _authorized: CampaignExecuteDep,
    request: Request,
) -> Campaign:
    return await _service(request).cancel_campaign(context.organization_id, campaign_id)


@campaigns_router.get("/campaigns/{campaign_id}/audience", response_model=list[CampaignRecipient])
@campaigns_router.get("/campaigns/{campaign_id}/recipients", response_model=list[CampaignRecipient])
async def campaign_audience(
    campaign_id: UUID,
    context: TenantContextDep,
    _authorized: CampaignReadDep,
    request: Request,
    limit: _Limit = 50,
    offset: _Offset = 0,
) -> list[CampaignRecipient]:
    return await _service(request).audience(
        context.organization_id, campaign_id, limit=limit, offset=offset
    )


@campaigns_router.get("/campaigns/{campaign_id}/runs", response_model=list[CampaignRun])
async def campaign_runs(
    campaign_id: UUID,
    context: TenantContextDep,
    _authorized: CampaignReadDep,
    request: Request,
    limit: _Limit = 50,
) -> list[CampaignRun]:
    return await _service(request).runs(context.organization_id, campaign_id, limit=limit)


@campaigns_router.get(
    "/campaigns/{campaign_id}/transitions", response_model=list[CampaignTransition]
)
async def campaign_transitions(
    campaign_id: UUID,
    context: TenantContextDep,
    _authorized: CampaignReadDep,
    request: Request,
    limit: _Limit = 100,
) -> list[CampaignTransition]:
    return await _service(request).transitions(context.organization_id, campaign_id, limit=limit)


@campaigns_router.put("/campaign-contact-preferences")
async def set_campaign_contact_preference(
    payload: ContactPreferenceRequest,
    context: TenantContextDep,
    _authorized: CampaignConfigureDep,
    request: Request,
) -> dict[str, int]:
    epoch = await _service(request).set_contact_preference(context.organization_id, payload)
    return {"consent_epoch": epoch}


@campaigns_router.post("/campaign-suppressions", status_code=status.HTTP_201_CREATED)
async def create_campaign_suppression(
    payload: SuppressionRequest,
    context: TenantContextDep,
    _authorized: CampaignConfigureDep,
    request: Request,
) -> dict[str, int]:
    epoch = await _service(request).add_suppression(context.organization_id, payload)
    return {"suppression_epoch": epoch}
