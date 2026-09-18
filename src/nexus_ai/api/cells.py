"""Bounded platform-control administration; Cell IDs never authorize tenant access."""

from typing import Annotated
from uuid import UUID, uuid7

from fastapi import APIRouter, Query, Request

from nexus_ai.api.auth_deps import LivePrincipalDep
from nexus_ai.api.dependencies import TenantContextDep, get_resources
from nexus_ai.cells.contracts import (
    CellView,
    PlacementMutation,
    PlacementPage,
    PlacementSnapshot,
    RegisterCellRequest,
    RetireCellRequest,
)

cells_router = APIRouter(tags=["cell-control"])


@cells_router.post("/cells", response_model=CellView, status_code=201)
async def register_cell(
    payload: RegisterCellRequest, principal: LivePrincipalDep, request: Request
) -> CellView:
    service = get_resources(request).cells
    cell_id = await service.register(principal.user_id, payload, uuid7())
    return await service.inspect_cell(principal.user_id, cell_id)


@cells_router.get("/cells", response_model=list[CellView])
async def list_cells(
    principal: LivePrincipalDep,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    after_id: UUID | None = None,
) -> list[CellView]:
    return await get_resources(request).cells.list_cells(
        principal.user_id, PlacementPage(limit=limit, after_id=after_id)
    )


@cells_router.get("/cells/{cell_id}", response_model=CellView)
async def inspect_cell(cell_id: UUID, principal: LivePrincipalDep, request: Request) -> CellView:
    return await get_resources(request).cells.inspect_cell(principal.user_id, cell_id)


@cells_router.post("/cells/retire", response_model=CellView)
async def retire_cell(
    payload: RetireCellRequest, principal: LivePrincipalDep, request: Request
) -> CellView:
    return await get_resources(request).cells.retire(principal.user_id, payload, uuid7())


@cells_router.get("/cell-placement/current", response_model=PlacementSnapshot)
async def inspect_placement(
    context: TenantContextDep, principal: LivePrincipalDep, request: Request
) -> PlacementSnapshot:
    return await get_resources(request).cells.inspect_placement(
        context.organization_id, principal.user_id
    )


@cells_router.post("/cell-placement/current", response_model=PlacementSnapshot)
async def mutate_placement(
    payload: PlacementMutation,
    context: TenantContextDep,
    principal: LivePrincipalDep,
    request: Request,
) -> PlacementSnapshot:
    return await get_resources(request).cells.mutate(
        context.organization_id, principal.user_id, payload, uuid7()
    )
