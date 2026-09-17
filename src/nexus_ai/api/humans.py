"""Tenant-scoped Human Agent Operations API (NXS-P17)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, status

from nexus_ai.api.auth_deps import PrincipalDep
from nexus_ai.api.authorization import (
    HumanConfigureDep,
    HumanReadDep,
    HumanSuperviseDep,
    HumanWorkDep,
)
from nexus_ai.api.dependencies import TenantContextDep, get_resources
from nexus_ai.humans.entities import (
    ActionAuthorization,
    AgentPresence,
    AssignmentActionRequest,
    ClaimResult,
    ClaimWorkRequest,
    ConversationOwnership,
    CopilotRequest,
    CopilotSuggestion,
    CreateQueueRequest,
    HumanAssignment,
    HumanQueue,
    HumanSendRequest,
    HumanTransition,
    HumanWorkItem,
    QueueHumanWorkRequest,
    ReturnToAiRequest,
    SetPresenceRequest,
    SupervisorActionRequest,
    SupervisorTransferToAgentRequest,
    TransferToAgentRequest,
    TransferToQueueRequest,
    UpdateQueueRequest,
)
from nexus_ai.humans.service import HumanOperationsService

humans_router = APIRouter(tags=["human-operations"])
_Limit = Annotated[int, Query(ge=1, le=100)]
_Offset = Annotated[int, Query(ge=0, le=1_000_000)]


def _service(request: Request) -> HumanOperationsService:
    return get_resources(request).humans


@humans_router.post("/human-queues", response_model=HumanQueue, status_code=status.HTTP_201_CREATED)
async def create_queue(
    payload: CreateQueueRequest,
    context: TenantContextDep,
    _authorized: HumanConfigureDep,
    request: Request,
) -> HumanQueue:
    return await _service(request).create_queue(context.organization_id, payload)


@humans_router.get("/human-queues", response_model=list[HumanQueue])
async def list_queues(
    context: TenantContextDep,
    _authorized: HumanReadDep,
    request: Request,
    limit: _Limit = 50,
    offset: _Offset = 0,
) -> list[HumanQueue]:
    return await _service(request).list_queues(context.organization_id, limit=limit, offset=offset)


@humans_router.get("/human-queues/{queue_id}", response_model=HumanQueue)
async def get_queue(
    queue_id: UUID, context: TenantContextDep, _authorized: HumanReadDep, request: Request
) -> HumanQueue:
    return await _service(request).get_queue(context.organization_id, queue_id)


@humans_router.patch("/human-queues/{queue_id}", response_model=HumanQueue)
async def update_queue(
    queue_id: UUID,
    payload: UpdateQueueRequest,
    context: TenantContextDep,
    _authorized: HumanConfigureDep,
    request: Request,
) -> HumanQueue:
    return await _service(request).update_queue(context.organization_id, queue_id, payload)


@humans_router.put("/human-agents/me/presence", response_model=AgentPresence)
async def set_my_presence(
    payload: SetPresenceRequest,
    context: TenantContextDep,
    principal: PrincipalDep,
    _authorized: HumanWorkDep,
    request: Request,
) -> AgentPresence:
    return await _service(request).set_presence(
        context.organization_id, principal.user_id, payload, actor_user_id=principal.user_id
    )


@humans_router.put("/human-agents/{agent_user_id}/presence", response_model=AgentPresence)
async def set_agent_presence(
    agent_user_id: UUID,
    payload: SetPresenceRequest,
    context: TenantContextDep,
    principal: PrincipalDep,
    _authorized: HumanSuperviseDep,
    request: Request,
) -> AgentPresence:
    return await _service(request).set_presence(
        context.organization_id, agent_user_id, payload, actor_user_id=principal.user_id
    )


@humans_router.get("/human-agents/presence", response_model=list[AgentPresence])
async def list_presence(
    context: TenantContextDep,
    _authorized: HumanReadDep,
    request: Request,
    limit: _Limit = 50,
    offset: _Offset = 0,
) -> list[AgentPresence]:
    return await _service(request).list_presence(
        context.organization_id, limit=limit, offset=offset
    )


@humans_router.post(
    "/human-work-items", response_model=HumanWorkItem, status_code=status.HTTP_201_CREATED
)
async def queue_work(
    payload: QueueHumanWorkRequest,
    context: TenantContextDep,
    principal: PrincipalDep,
    _authorized: HumanWorkDep,
    request: Request,
) -> HumanWorkItem:
    return await _service(request).request_ai_handoff(
        context.organization_id, payload, actor_user_id=principal.user_id
    )


@humans_router.get("/human-work-items", response_model=list[HumanWorkItem])
async def list_work(
    context: TenantContextDep,
    _authorized: HumanReadDep,
    request: Request,
    queue_id: UUID | None = None,
    limit: _Limit = 50,
    offset: _Offset = 0,
) -> list[HumanWorkItem]:
    return await _service(request).list_work(
        context.organization_id, queue_id=queue_id, limit=limit, offset=offset
    )


@humans_router.get("/human-work-items/{work_item_id}", response_model=HumanWorkItem)
async def get_work(
    work_item_id: UUID, context: TenantContextDep, _authorized: HumanReadDep, request: Request
) -> HumanWorkItem:
    return await _service(request).get_work_item(context.organization_id, work_item_id)


@humans_router.post("/human-work-items/claim", response_model=ClaimResult)
async def claim_work(
    payload: ClaimWorkRequest,
    context: TenantContextDep,
    principal: PrincipalDep,
    _authorized: HumanWorkDep,
    request: Request,
) -> ClaimResult:
    return await _service(request).claim_work(context.organization_id, principal.user_id, payload)


@humans_router.post("/human-assignments/{assignment_id}/accept", response_model=HumanWorkItem)
async def accept(
    assignment_id: UUID,
    payload: AssignmentActionRequest,
    context: TenantContextDep,
    principal: PrincipalDep,
    _authorized: HumanWorkDep,
    request: Request,
) -> HumanWorkItem:
    return await _service(request).accept_assignment(
        context.organization_id, assignment_id, payload, actor_user_id=principal.user_id
    )


@humans_router.post("/human-assignments/{assignment_id}/complete", response_model=HumanWorkItem)
async def complete(
    assignment_id: UUID,
    payload: AssignmentActionRequest,
    context: TenantContextDep,
    principal: PrincipalDep,
    _authorized: HumanWorkDep,
    request: Request,
) -> HumanWorkItem:
    return await _service(request).complete_assignment(
        context.organization_id, assignment_id, payload, actor_user_id=principal.user_id
    )


@humans_router.post(
    "/human-assignments/{assignment_id}/transfer/queue", response_model=HumanWorkItem
)
async def transfer_queue(
    assignment_id: UUID,
    payload: TransferToQueueRequest,
    context: TenantContextDep,
    principal: PrincipalDep,
    _authorized: HumanWorkDep,
    request: Request,
) -> HumanWorkItem:
    return await _service(request).transfer_to_queue(
        context.organization_id, assignment_id, payload, actor_user_id=principal.user_id
    )


@humans_router.post("/human-assignments/{assignment_id}/transfer/agent", response_model=ClaimResult)
async def transfer_agent(
    assignment_id: UUID,
    payload: TransferToAgentRequest,
    context: TenantContextDep,
    principal: PrincipalDep,
    _authorized: HumanWorkDep,
    request: Request,
) -> ClaimResult:
    return await _service(request).transfer_to_agent(
        context.organization_id, assignment_id, payload, actor_user_id=principal.user_id
    )


@humans_router.post(
    "/human-assignments/{assignment_id}/supervisor-release", response_model=HumanWorkItem
)
async def supervisor_release(
    assignment_id: UUID,
    payload: SupervisorActionRequest,
    context: TenantContextDep,
    principal: PrincipalDep,
    _authorized: HumanSuperviseDep,
    request: Request,
) -> HumanWorkItem:
    return await _service(request).supervisor_release(
        context.organization_id, assignment_id, payload, actor_user_id=principal.user_id
    )


@humans_router.post(
    "/human-assignments/{assignment_id}/supervisor-transfer/agent",
    response_model=ClaimResult,
)
async def supervisor_transfer_agent(
    assignment_id: UUID,
    payload: SupervisorTransferToAgentRequest,
    context: TenantContextDep,
    principal: PrincipalDep,
    _authorized: HumanSuperviseDep,
    request: Request,
) -> ClaimResult:
    return await _service(request).supervisor_transfer_to_agent(
        context.organization_id, assignment_id, payload, actor_user_id=principal.user_id
    )


@humans_router.post("/human-work-items/{work_item_id}/cancel", response_model=HumanWorkItem)
async def cancel_work(
    work_item_id: UUID,
    payload: SupervisorActionRequest,
    context: TenantContextDep,
    principal: PrincipalDep,
    _authorized: HumanSuperviseDep,
    request: Request,
) -> HumanWorkItem:
    return await _service(request).cancel_work(
        context.organization_id, work_item_id, payload, actor_user_id=principal.user_id
    )


@humans_router.post(
    "/human-assignments/{assignment_id}/return-to-ai", response_model=ConversationOwnership
)
async def return_to_ai(
    assignment_id: UUID,
    payload: ReturnToAiRequest,
    context: TenantContextDep,
    principal: PrincipalDep,
    _authorized: HumanWorkDep,
    request: Request,
) -> ConversationOwnership:
    return await _service(request).return_to_ai(
        context.organization_id, assignment_id, payload, principal=principal
    )


@humans_router.post(
    "/human-assignments/{assignment_id}/messages", response_model=ActionAuthorization
)
async def send_message(
    assignment_id: UUID,
    payload: HumanSendRequest,
    context: TenantContextDep,
    principal: PrincipalDep,
    _authorized: HumanWorkDep,
    request: Request,
) -> ActionAuthorization:
    return await _service(request).send_message(
        context.organization_id, assignment_id, payload, actor_user_id=principal.user_id
    )


@humans_router.post("/human-assignments/{assignment_id}/copilot", response_model=CopilotSuggestion)
async def copilot(
    assignment_id: UUID,
    payload: CopilotRequest,
    context: TenantContextDep,
    principal: PrincipalDep,
    _authorized: HumanWorkDep,
    request: Request,
) -> CopilotSuggestion:
    return await _service(request).copilot(
        context.organization_id, assignment_id, payload, principal=principal
    )


@humans_router.get("/human-assignments", response_model=list[HumanAssignment])
async def list_assignments(
    context: TenantContextDep,
    _authorized: HumanReadDep,
    request: Request,
    limit: _Limit = 50,
    offset: _Offset = 0,
) -> list[HumanAssignment]:
    return await _service(request).list_assignments(
        context.organization_id, limit=limit, offset=offset
    )


@humans_router.get(
    "/conversations/{conversation_id}/ownership", response_model=ConversationOwnership
)
async def ownership(
    conversation_id: UUID, context: TenantContextDep, _authorized: HumanReadDep, request: Request
) -> ConversationOwnership:
    return await _service(request).get_ownership(context.organization_id, conversation_id)


@humans_router.get("/human-entities/{entity_id}/transitions", response_model=list[HumanTransition])
async def transitions(
    entity_id: UUID,
    context: TenantContextDep,
    _authorized: HumanReadDep,
    request: Request,
    limit: _Limit = 100,
) -> list[HumanTransition]:
    return await _service(request).transitions(context.organization_id, entity_id, limit=limit)
