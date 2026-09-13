"""Tenant-scoped Scheduler management API (NXS-P15)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, status

from nexus_ai.api.authorization import ScheduleConfigureDep, ScheduleExecuteDep, ScheduleReadDep
from nexus_ai.api.dependencies import TenantContextDep, get_resources
from nexus_ai.scheduler.entities import (
    CreateScheduleRequest,
    Schedule,
    ScheduleOccurrence,
    ScheduleTransition,
    UpdateScheduleRequest,
)
from nexus_ai.scheduler.service import SchedulerService

schedules_router = APIRouter(tags=["scheduler"])
_Limit = Annotated[int, Query(ge=1, le=100)]
_Offset = Annotated[int, Query(ge=0, le=1_000_000)]


def _service(request: Request) -> SchedulerService:
    return get_resources(request).scheduler


@schedules_router.post("/schedules", response_model=Schedule, status_code=status.HTTP_201_CREATED)
async def create_schedule(
    payload: CreateScheduleRequest,
    context: TenantContextDep,
    _authorized: ScheduleConfigureDep,
    request: Request,
) -> Schedule:
    return await _service(request).create_schedule(context.organization_id, payload)


@schedules_router.get("/schedules", response_model=list[Schedule])
async def list_schedules(
    context: TenantContextDep,
    _authorized: ScheduleReadDep,
    request: Request,
    limit: _Limit = 50,
    offset: _Offset = 0,
) -> list[Schedule]:
    return await _service(request).list_schedules(
        context.organization_id, limit=limit, offset=offset
    )


@schedules_router.get("/schedules/{schedule_id}", response_model=Schedule)
async def get_schedule(
    schedule_id: UUID,
    context: TenantContextDep,
    _authorized: ScheduleReadDep,
    request: Request,
) -> Schedule:
    return await _service(request).get_schedule(context.organization_id, schedule_id)


@schedules_router.patch("/schedules/{schedule_id}", response_model=Schedule)
async def update_schedule(
    schedule_id: UUID,
    payload: UpdateScheduleRequest,
    context: TenantContextDep,
    _authorized: ScheduleConfigureDep,
    request: Request,
) -> Schedule:
    return await _service(request).update_schedule(context.organization_id, schedule_id, payload)


@schedules_router.post("/schedules/{schedule_id}/activate", response_model=Schedule)
async def activate_schedule(
    schedule_id: UUID,
    context: TenantContextDep,
    _authorized: ScheduleExecuteDep,
    request: Request,
) -> Schedule:
    return await _service(request).activate_schedule(context.organization_id, schedule_id)


@schedules_router.post("/schedules/{schedule_id}/pause", response_model=Schedule)
async def pause_schedule(
    schedule_id: UUID,
    context: TenantContextDep,
    _authorized: ScheduleExecuteDep,
    request: Request,
) -> Schedule:
    return await _service(request).pause_schedule(context.organization_id, schedule_id)


@schedules_router.post("/schedules/{schedule_id}/resume", response_model=Schedule)
async def resume_schedule(
    schedule_id: UUID,
    context: TenantContextDep,
    _authorized: ScheduleExecuteDep,
    request: Request,
) -> Schedule:
    return await _service(request).resume_schedule(context.organization_id, schedule_id)


@schedules_router.post("/schedules/{schedule_id}/cancel", response_model=Schedule)
async def cancel_schedule(
    schedule_id: UUID,
    context: TenantContextDep,
    _authorized: ScheduleExecuteDep,
    request: Request,
) -> Schedule:
    return await _service(request).cancel_schedule(context.organization_id, schedule_id)


@schedules_router.get(
    "/schedules/{schedule_id}/occurrences", response_model=list[ScheduleOccurrence]
)
async def list_occurrences(
    schedule_id: UUID,
    context: TenantContextDep,
    _authorized: ScheduleReadDep,
    request: Request,
    limit: _Limit = 50,
) -> list[ScheduleOccurrence]:
    return await _service(request).list_occurrences(
        context.organization_id, schedule_id, limit=limit
    )


@schedules_router.get("/schedule-occurrences/{occurrence_id}", response_model=ScheduleOccurrence)
async def get_occurrence(
    occurrence_id: UUID,
    context: TenantContextDep,
    _authorized: ScheduleReadDep,
    request: Request,
) -> ScheduleOccurrence:
    return await _service(request).get_occurrence(context.organization_id, occurrence_id)


@schedules_router.post(
    "/schedule-occurrences/{occurrence_id}/cancel", response_model=ScheduleOccurrence
)
async def cancel_occurrence(
    occurrence_id: UUID,
    context: TenantContextDep,
    _authorized: ScheduleExecuteDep,
    request: Request,
) -> ScheduleOccurrence:
    return await _service(request).cancel_occurrence(context.organization_id, occurrence_id)


@schedules_router.get("/schedules/{entity_id}/transitions", response_model=list[ScheduleTransition])
async def schedule_transitions(
    entity_id: UUID,
    context: TenantContextDep,
    _authorized: ScheduleReadDep,
    request: Request,
    limit: _Limit = 100,
) -> list[ScheduleTransition]:
    return await _service(request).transitions(context.organization_id, entity_id, limit=limit)
