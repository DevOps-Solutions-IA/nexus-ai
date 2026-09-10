"""AI Agent Runtime API surface (NXS-P13: NXS-AGENT-001).

Governed operations only: model-provider-account + model-profile + agent CRUD, and the
agent-session / turn lifecycle. There is deliberately NO arbitrary model endpoint, raw
API key, raw system-prompt injection from a runtime caller, unrestricted tool surface,
raw provider payload, hidden reasoning, arbitrary HTTP URL, shell or SQL. Every request
model is ``extra="forbid"`` and bounded.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, status

from nexus_ai.agents.entities import (
    AgentResponse,
    AgentSessionView,
    AgentTurnView,
    AgentView,
    CreateAgentRequest,
    CreateModelProfileRequest,
    ModelProfileView,
    ModelProviderAccountView,
    RegisterModelProviderAccountRequest,
    StartAgentSessionRequest,
    StopAgentSessionRequest,
    StoreModelCredentialRequest,
    SubmitTurnRequest,
    UpdateAgentRequest,
    UpdateModelProfileRequest,
    UpdateModelProviderAccountRequest,
)
from nexus_ai.agents.service import AgentService
from nexus_ai.api.auth_deps import PrincipalDep
from nexus_ai.api.authorization import AiConfigureDep, AiReadDep, AiUseDep
from nexus_ai.api.dependencies import TenantContextDep, get_resources

agents_router = APIRouter(prefix="/agents", tags=["agents"])

_Limit = Annotated[int, Query(ge=1, le=100)]


def _svc(request: Request) -> AgentService:
    return get_resources(request).agents


# --- model provider accounts ------------------------------------------------


@agents_router.post(
    "/model-accounts",
    response_model=ModelProviderAccountView,
    status_code=status.HTTP_201_CREATED,
)
async def create_model_account(
    payload: RegisterModelProviderAccountRequest,
    context: TenantContextDep,
    _authorized: AiConfigureDep,
    request: Request,
) -> ModelProviderAccountView:
    account = await _svc(request).create_account(context.organization_id, payload)
    return account.public_view()


@agents_router.get("/model-accounts", response_model=list[ModelProviderAccountView])
async def list_model_accounts(
    context: TenantContextDep,
    _authorized: AiReadDep,
    request: Request,
    limit: _Limit = 50,
) -> list[ModelProviderAccountView]:
    rows = await _svc(request).list_accounts(context.organization_id, limit=limit)
    return [row.public_view() for row in rows]


@agents_router.get("/model-accounts/{account_id}", response_model=ModelProviderAccountView)
async def get_model_account(
    account_id: UUID,
    context: TenantContextDep,
    _authorized: AiReadDep,
    request: Request,
) -> ModelProviderAccountView:
    account = await _svc(request).get_account(context.organization_id, account_id)
    return account.public_view()


@agents_router.patch("/model-accounts/{account_id}", response_model=ModelProviderAccountView)
async def update_model_account(
    account_id: UUID,
    payload: UpdateModelProviderAccountRequest,
    context: TenantContextDep,
    _authorized: AiConfigureDep,
    request: Request,
) -> ModelProviderAccountView:
    account = await _svc(request).update_account(context.organization_id, account_id, payload)
    return account.public_view()


@agents_router.post(
    "/model-accounts/{account_id}/credentials", status_code=status.HTTP_204_NO_CONTENT
)
async def store_model_credential(
    account_id: UUID,
    payload: StoreModelCredentialRequest,
    context: TenantContextDep,
    _authorized: AiConfigureDep,
    request: Request,
) -> None:
    await _svc(request).store_account_credential(context.organization_id, account_id, payload)


# --- model profiles -------------------------------------------------------


@agents_router.post(
    "/model-profiles", response_model=ModelProfileView, status_code=status.HTTP_201_CREATED
)
async def create_model_profile(
    payload: CreateModelProfileRequest,
    context: TenantContextDep,
    _authorized: AiConfigureDep,
    request: Request,
) -> ModelProfileView:
    profile = await _svc(request).create_profile(context.organization_id, payload)
    return profile.public_view()


@agents_router.get("/model-profiles", response_model=list[ModelProfileView])
async def list_model_profiles(
    context: TenantContextDep,
    _authorized: AiReadDep,
    request: Request,
    limit: _Limit = 50,
) -> list[ModelProfileView]:
    rows = await _svc(request).list_profiles(context.organization_id, limit=limit)
    return [row.public_view() for row in rows]


@agents_router.get("/model-profiles/{profile_id}", response_model=ModelProfileView)
async def get_model_profile(
    profile_id: UUID,
    context: TenantContextDep,
    _authorized: AiReadDep,
    request: Request,
) -> ModelProfileView:
    profile = await _svc(request).get_profile(context.organization_id, profile_id)
    return profile.public_view()


@agents_router.patch("/model-profiles/{profile_id}", response_model=ModelProfileView)
async def update_model_profile(
    profile_id: UUID,
    payload: UpdateModelProfileRequest,
    context: TenantContextDep,
    _authorized: AiConfigureDep,
    request: Request,
) -> ModelProfileView:
    profile = await _svc(request).update_profile(context.organization_id, profile_id, payload)
    return profile.public_view()


# --- agents -----------------------------------------------------------


@agents_router.post("", response_model=AgentView, status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: CreateAgentRequest,
    context: TenantContextDep,
    _authorized: AiConfigureDep,
    request: Request,
) -> AgentView:
    agent = await _svc(request).create_agent(context.organization_id, payload)
    return agent.public_view()


@agents_router.get("", response_model=list[AgentView])
async def list_agents(
    context: TenantContextDep,
    _authorized: AiReadDep,
    request: Request,
    limit: _Limit = 50,
) -> list[AgentView]:
    rows = await _svc(request).list_agents(context.organization_id, limit=limit)
    return [row.public_view() for row in rows]


# --- sessions & turns -------------------------------------------------
#
# NOTE: the literal ``/sessions`` routes are declared BEFORE the ``/{agent_id}`` routes
# so ``GET /agents/sessions`` is never captured by the agent-detail path parameter.


@agents_router.post(
    "/sessions", response_model=AgentSessionView, status_code=status.HTTP_201_CREATED
)
async def start_session(
    payload: StartAgentSessionRequest,
    context: TenantContextDep,
    principal: PrincipalDep,
    _authorized: AiUseDep,
    request: Request,
) -> AgentSessionView:
    session = await _svc(request).start_session(context.organization_id, principal, payload)
    return session.public_view()


@agents_router.get("/sessions", response_model=list[AgentSessionView])
async def list_sessions(
    context: TenantContextDep,
    _authorized: AiReadDep,
    request: Request,
    agent_id: UUID | None = None,
    limit: _Limit = 50,
) -> list[AgentSessionView]:
    rows = await _svc(request).list_sessions(
        context.organization_id, agent_id=agent_id, limit=limit
    )
    return [row.public_view() for row in rows]


@agents_router.get("/sessions/{session_id}", response_model=AgentSessionView)
async def get_session(
    session_id: UUID,
    context: TenantContextDep,
    _authorized: AiReadDep,
    request: Request,
) -> AgentSessionView:
    session = await _svc(request).get_session(context.organization_id, session_id)
    return session.public_view()


@agents_router.post("/sessions/{session_id}/turns", response_model=AgentResponse)
async def submit_turn(
    session_id: UUID,
    payload: SubmitTurnRequest,
    context: TenantContextDep,
    _authorized: AiUseDep,
    request: Request,
) -> AgentResponse:
    return await _svc(request).submit_turn(context.organization_id, session_id, payload)


@agents_router.get("/sessions/{session_id}/turns", response_model=list[AgentTurnView])
async def list_turns(
    session_id: UUID,
    context: TenantContextDep,
    _authorized: AiReadDep,
    request: Request,
    limit: _Limit = 50,
) -> list[AgentTurnView]:
    rows = await _svc(request).list_turns(context.organization_id, session_id, limit=limit)
    return [row.public_view() for row in rows]


@agents_router.post("/sessions/{session_id}/stop", response_model=AgentSessionView)
async def stop_session(
    session_id: UUID,
    payload: StopAgentSessionRequest,
    context: TenantContextDep,
    _authorized: AiUseDep,
    request: Request,
) -> AgentSessionView:
    session = await _svc(request).stop_session(context.organization_id, session_id, payload)
    return session.public_view()


@agents_router.post("/sessions/{session_id}/cancel", response_model=AgentSessionView)
async def cancel_session(
    session_id: UUID,
    context: TenantContextDep,
    _authorized: AiUseDep,
    request: Request,
) -> AgentSessionView:
    session = await _svc(request).cancel_session(context.organization_id, session_id)
    return session.public_view()


# --- agent detail (declared last: ``/{agent_id}`` must not shadow literal paths) ----


@agents_router.get("/{agent_id}", response_model=AgentView)
async def get_agent(
    agent_id: UUID,
    context: TenantContextDep,
    _authorized: AiReadDep,
    request: Request,
) -> AgentView:
    agent = await _svc(request).get_agent(context.organization_id, agent_id)
    return agent.public_view()


@agents_router.patch("/{agent_id}", response_model=AgentView)
async def update_agent(
    agent_id: UUID,
    payload: UpdateAgentRequest,
    context: TenantContextDep,
    _authorized: AiConfigureDep,
    request: Request,
) -> AgentView:
    agent = await _svc(request).update_agent(context.organization_id, agent_id, payload)
    return agent.public_view()
