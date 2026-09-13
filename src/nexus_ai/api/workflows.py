"""Workflow Engine management and execution-control API (NXS-P14)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, status

from nexus_ai.api.authorization import (
    WorkflowConfigureDep,
    WorkflowExecuteDep,
    WorkflowReadDep,
)
from nexus_ai.api.dependencies import TenantContextDep, get_resources
from nexus_ai.workflows.entities import (
    CreateWorkflowRequest,
    StartWorkflowRunRequest,
    UpdateWorkflowRequest,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowStepRun,
    WorkflowTransition,
    WorkflowVersion,
)
from nexus_ai.workflows.service import WorkflowService

workflows_router = APIRouter(tags=["workflows"])
_Limit = Annotated[int, Query(ge=1, le=100)]
_Offset = Annotated[int, Query(ge=0, le=1_000_000)]


def _service(request: Request) -> WorkflowService:
    return get_resources(request).workflows


@workflows_router.post(
    "/workflows", response_model=WorkflowDefinition, status_code=status.HTTP_201_CREATED
)
async def create_workflow(
    payload: CreateWorkflowRequest,
    context: TenantContextDep,
    _authorized: WorkflowConfigureDep,
    request: Request,
) -> WorkflowDefinition:
    return await _service(request).create_definition(context.organization_id, payload)


@workflows_router.get("/workflows", response_model=list[WorkflowDefinition])
async def list_workflows(
    context: TenantContextDep,
    _authorized: WorkflowReadDep,
    request: Request,
    limit: _Limit = 50,
    offset: _Offset = 0,
) -> list[WorkflowDefinition]:
    return await _service(request).list_definitions(
        context.organization_id, limit=limit, offset=offset
    )


@workflows_router.get("/workflows/{definition_id}", response_model=WorkflowDefinition)
async def get_workflow(
    definition_id: UUID,
    context: TenantContextDep,
    _authorized: WorkflowReadDep,
    request: Request,
) -> WorkflowDefinition:
    return await _service(request).get_definition(context.organization_id, definition_id)


@workflows_router.patch("/workflows/{definition_id}", response_model=WorkflowDefinition)
async def update_workflow(
    definition_id: UUID,
    payload: UpdateWorkflowRequest,
    context: TenantContextDep,
    _authorized: WorkflowConfigureDep,
    request: Request,
) -> WorkflowDefinition:
    return await _service(request).update_definition(
        context.organization_id, definition_id, payload
    )


@workflows_router.post("/workflows/{definition_id}/publish", response_model=WorkflowVersion)
async def publish_workflow(
    definition_id: UUID,
    context: TenantContextDep,
    _authorized: WorkflowConfigureDep,
    request: Request,
) -> WorkflowVersion:
    return await _service(request).publish(context.organization_id, definition_id)


@workflows_router.get("/workflows/{definition_id}/versions", response_model=list[WorkflowVersion])
async def list_workflow_versions(
    definition_id: UUID,
    context: TenantContextDep,
    _authorized: WorkflowReadDep,
    request: Request,
    limit: _Limit = 50,
) -> list[WorkflowVersion]:
    return await _service(request).list_versions(
        context.organization_id, definition_id, limit=limit
    )


@workflows_router.get(
    "/workflows/{definition_id}/versions/{version_id}", response_model=WorkflowVersion
)
async def get_workflow_version(
    definition_id: UUID,
    version_id: UUID,
    context: TenantContextDep,
    _authorized: WorkflowReadDep,
    request: Request,
) -> WorkflowVersion:
    version = await _service(request).get_version(context.organization_id, version_id)
    if version.definition_id != definition_id:
        from nexus_ai.workflows.errors import WorkflowVersionNotFoundError

        raise WorkflowVersionNotFoundError("version does not belong to this workflow")
    return version


@workflows_router.post(
    "/workflow-runs", response_model=WorkflowRun, status_code=status.HTTP_201_CREATED
)
async def start_workflow_run(
    payload: StartWorkflowRunRequest,
    context: TenantContextDep,
    _authorized: WorkflowExecuteDep,
    request: Request,
) -> WorkflowRun:
    return await _service(request).start_run(context.organization_id, payload)


@workflows_router.get("/workflow-runs", response_model=list[WorkflowRun])
async def list_workflow_runs(
    context: TenantContextDep,
    _authorized: WorkflowReadDep,
    request: Request,
    limit: _Limit = 50,
    offset: _Offset = 0,
) -> list[WorkflowRun]:
    return await _service(request).list_runs(context.organization_id, limit=limit, offset=offset)


@workflows_router.get("/workflow-runs/{run_id}", response_model=WorkflowRun)
async def get_workflow_run(
    run_id: UUID,
    context: TenantContextDep,
    _authorized: WorkflowReadDep,
    request: Request,
) -> WorkflowRun:
    return await _service(request).get_run(context.organization_id, run_id)


@workflows_router.post("/workflow-runs/{run_id}/pause", response_model=WorkflowRun)
async def pause_workflow_run(
    run_id: UUID,
    context: TenantContextDep,
    _authorized: WorkflowExecuteDep,
    request: Request,
) -> WorkflowRun:
    return await _service(request).pause_run(context.organization_id, run_id)


@workflows_router.post("/workflow-runs/{run_id}/resume", response_model=WorkflowRun)
async def resume_workflow_run(
    run_id: UUID,
    context: TenantContextDep,
    _authorized: WorkflowExecuteDep,
    request: Request,
) -> WorkflowRun:
    return await _service(request).resume_run(context.organization_id, run_id)


@workflows_router.post("/workflow-runs/{run_id}/cancel", response_model=WorkflowRun)
async def cancel_workflow_run(
    run_id: UUID,
    context: TenantContextDep,
    _authorized: WorkflowExecuteDep,
    request: Request,
) -> WorkflowRun:
    return await _service(request).cancel_run(context.organization_id, run_id)


@workflows_router.get("/workflow-runs/{run_id}/steps", response_model=list[WorkflowStepRun])
async def list_workflow_run_steps(
    run_id: UUID,
    context: TenantContextDep,
    _authorized: WorkflowReadDep,
    request: Request,
) -> list[WorkflowStepRun]:
    return await _service(request).list_steps(context.organization_id, run_id)


@workflows_router.get(
    "/workflow-runs/{run_id}/transitions", response_model=list[WorkflowTransition]
)
async def list_workflow_transitions(
    run_id: UUID,
    context: TenantContextDep,
    _authorized: WorkflowReadDep,
    request: Request,
    limit: _Limit = 100,
) -> list[WorkflowTransition]:
    return await _service(request).transitions(context.organization_id, run_id, limit=limit)
