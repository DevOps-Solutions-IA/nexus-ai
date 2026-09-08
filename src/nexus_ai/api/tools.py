"""Tool Engine API surface (NXS-TOOL-001).

Governed registry operations plus one invocation endpoint. There is deliberately NO
generic arbitrary-execution endpoint: a caller registers a deterministic, versioned tool
and later invokes it by ``tool_key`` with validated ``arguments`` — never an
``integration_id``, a URL, an HTTP method, a header set, an ``operation_key`` or a
GraphQL document.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status

from nexus_ai.api.auth_deps import PrincipalDep
from nexus_ai.api.authorization import (
    ToolCreateDep,
    ToolDisableDep,
    ToolInvokeDep,
    ToolReadDep,
    ToolUpdateDep,
)
from nexus_ai.api.dependencies import TenantContextDep, get_resources
from nexus_ai.tools.entities import (
    RegisterToolRequest,
    ToolDefinitionView,
    ToolInvocation,
    ToolStatus,
    UpdateToolRequest,
)

tools_router = APIRouter(prefix="/tools", tags=["tools"])

_Limit = Annotated[int, Query(ge=1, le=100)]


@tools_router.post("", response_model=ToolDefinitionView, status_code=status.HTTP_201_CREATED)
async def register_tool(
    payload: RegisterToolRequest,
    context: TenantContextDep,
    _authorized: ToolCreateDep,
    request: Request,
) -> ToolDefinitionView:
    tool = await get_resources(request).tools.register(context.organization_id, payload)
    return tool.public_view()


@tools_router.get("", response_model=list[ToolDefinitionView])
async def list_tools(
    context: TenantContextDep,
    _authorized: ToolReadDep,
    request: Request,
    limit: _Limit = 20,
    after_id: UUID | None = None,
) -> list[ToolDefinitionView]:
    rows = await get_resources(request).tools.list_tools(
        context.organization_id, limit=limit, after_id=after_id
    )
    return [row.public_view() for row in rows]


@tools_router.get("/{tool_id}", response_model=ToolDefinitionView)
async def get_tool(
    tool_id: UUID,
    context: TenantContextDep,
    _authorized: ToolReadDep,
    request: Request,
) -> ToolDefinitionView:
    tool = await get_resources(request).tools.get(context.organization_id, tool_id)
    return tool.public_view()


@tools_router.patch("/{tool_id}", response_model=ToolDefinitionView)
async def update_tool(
    tool_id: UUID,
    payload: UpdateToolRequest,
    context: TenantContextDep,
    _authorized: ToolUpdateDep,
    request: Request,
) -> ToolDefinitionView:
    tool = await get_resources(request).tools.update(context.organization_id, tool_id, payload)
    return tool.public_view()


@tools_router.post("/{tool_id}/enable", response_model=ToolDefinitionView)
async def enable_tool(
    tool_id: UUID,
    context: TenantContextDep,
    _authorized: ToolDisableDep,
    request: Request,
) -> ToolDefinitionView:
    tool = await get_resources(request).tools.set_status(
        context.organization_id, tool_id, ToolStatus.ACTIVE
    )
    return tool.public_view()


@tools_router.post("/{tool_id}/disable", response_model=ToolDefinitionView)
async def disable_tool(
    tool_id: UUID,
    context: TenantContextDep,
    _authorized: ToolDisableDep,
    request: Request,
) -> ToolDefinitionView:
    tool = await get_resources(request).tools.set_status(
        context.organization_id, tool_id, ToolStatus.DISABLED
    )
    return tool.public_view()


@tools_router.delete("/{tool_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_tool(
    tool_id: UUID,
    context: TenantContextDep,
    _authorized: ToolUpdateDep,
    request: Request,
) -> Response:
    await get_resources(request).tools.remove(context.organization_id, tool_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@tools_router.post("/invoke")
async def invoke_tool(
    payload: ToolInvocation,
    principal: PrincipalDep,
    _authorized: ToolInvokeDep,
    request: Request,
) -> Any:
    # The trusted Organization is the principal's, never a payload field.
    result = await get_resources(request).tool_engine.invoke(principal, payload)
    return result.model_dump(mode="json")
