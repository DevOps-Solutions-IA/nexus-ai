"""NXS-P13 audit corrective #1 — turn-wide tool guarantees (blockers 1 & 2).

Against the real NXS-P08 Tool Engine + PostgreSQL + event outbox:
  * a semantically identical tool request repeated in LATER model iterations is executed
    exactly once — one external call, one durable execution record, one business effect;
  * ``max_tool_calls_per_turn`` is a TURN-wide budget accumulated across every iteration,
    enforced before the offending call reaches the Tool Engine.

These tests fail against the pre-corrective head (38c6dc7): the old ``seen`` cache was
re-created per iteration and the iteration index was folded into the P08 idempotency
key, and the only "per turn" check was really per model response.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.agents.entities import (
    CreateAgentRequest,
    StartAgentSessionRequest,
    SubmitTurnRequest,
)
from nexus_ai.agents.errors import AgentToolLoopLimitError
from nexus_ai.agents.models.fake import FakeModelTurn
from nexus_ai.integrations.entities import (
    CreateIntegrationRequest,
    HttpMethod,
    IntegrationStatus,
    IntegrationType,
    ParamSpec,
    RestOperationSpec,
    RetryClass,
    SetOperationRequest,
)
from nexus_ai.tools.entities import (
    RegisterToolRequest,
    RiskClass,
    SideEffectClass,
    ToolBinding,
    ToolIdempotencyPolicy,
    ToolStatus,
)
from tests.integration.test_agent_service import _TOOL_SCHEMA, _provision

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _register_write_tool(stack: Any, org_id: Any, mock: Any, key: str = "crm.create") -> None:
    """A NON_IDEMPOTENT_WRITE tool — a repeated identical call must NOT double-write."""
    integration = await stack.hub.registry.create(
        org_id,
        CreateIntegrationRequest(
            slug="crm", name="crm", integration_type=IntegrationType.REST, base_url=mock.base_url
        ),
    )
    await stack.hub.registry.set_status(org_id, integration.id, IntegrationStatus.ACTIVE)
    await stack.hub.registry.set_operation(
        org_id,
        integration.id,
        SetOperationRequest(
            operation_key="crm.create",
            spec=RestOperationSpec(
                method=HttpMethod.POST,
                path="/c/{id}",
                path_params={"id": ParamSpec(required=True)},
                retry_class=RetryClass.NON_IDEMPOTENT,
            ),
        ),
    )
    tool = await stack.tool_registry.register(
        org_id,
        RegisterToolRequest(
            tool_key=key,
            name="Create contact",
            description="Create a CRM contact",
            risk_class=RiskClass.MEDIUM,
            side_effect_class=SideEffectClass.NON_IDEMPOTENT_WRITE,
            idempotency_policy=ToolIdempotencyPolicy.REQUIRED,
            input_schema=_TOOL_SCHEMA,
            output_schema={"type": "object", "required": ["id"]},
            binding=ToolBinding(integration_id=integration.id, operation_key="crm.create"),
        ),
    )
    await stack.tool_registry.set_status(org_id, tool.id, ToolStatus.ACTIVE)


async def _external_calls(stack: Any, org_id: Any) -> int:
    async with stack.database.tenant_transaction(org_id) as tenant:
        return int(
            (
                await tenant.session.execute(text("SELECT count(*) FROM tool_execution_records"))
            ).scalar_one()
        )


@pytest.mark.parametrize("repeats", [2, 3, 5])
async def test_identical_tool_call_repeated_across_iterations_executes_once(
    agent_stack: Any,
    make_organization: Any,
    make_tool_principal: Any,
    mock_http_server: Any,
    repeats: int,
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_write_tool(agent_stack, org.id, mock_http_server)
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.create",))

    hits = {"n": 0}

    def _handler(method: str, path: str, headers: Any, body: Any) -> tuple[int, dict[str, Any]]:
        hits["n"] += 1
        return 201, {"id": "c-1"}

    mock_http_server.set_handler(_handler)

    args = {"path_params": {"id": "c-1"}}
    # the model asks for the SAME semantic call on `repeats` consecutive iterations,
    # each time with a DIFFERENT model-generated tool_call_id, then answers.
    for i in range(repeats):
        agent_stack.script.append(
            FakeModelTurn(tool_calls=(("crm.create", args),), tool_call_id_prefix=f"iter{i}")
        )
    agent_stack.script.append(FakeModelTurn(content="the contact already exists; done"))

    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    response = await agent_stack.service.submit_turn(
        org.id, session.id, SubmitTurnRequest(content="create the contact")
    )

    assert response.content == "the contact already exists; done"
    assert hits["n"] == 1  # exactly one outbound HTTP call
    assert await _external_calls(agent_stack, org.id) == 1  # one durable execution record

    async with agent_stack.database.tenant_transaction(org.id) as tenant:
        rows = (
            await tenant.session.execute(
                text("SELECT status, tool_key FROM ai_agent_tool_calls ORDER BY created_at")
            )
        ).all()
    assert [r[0] for r in rows] == ["COMPLETED"]  # one recorded tool call, succeeded
    assert rows[0][1] == "crm.create"

    turns = await agent_stack.service.list_turns(org.id, session.id, limit=5)
    assert turns[0].state.value == "COMPLETED"
    assert turns[0].tool_iterations == repeats  # every model iteration is accounted for


async def test_repeated_identical_call_reuses_the_first_p08_idempotency_key(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_write_tool(agent_stack, org.id, mock_http_server)
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.create",))
    mock_http_server.set_handler(lambda m, p, h, b: (201, {"id": "x"}))

    args = {"path_params": {"id": "x"}}
    agent_stack.script.append(
        FakeModelTurn(tool_calls=(("crm.create", args),), tool_call_id_prefix="a")
    )
    agent_stack.script.append(
        FakeModelTurn(tool_calls=(("crm.create", args),), tool_call_id_prefix="b")
    )
    agent_stack.script.append(FakeModelTurn(content="done"))

    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    await agent_stack.service.submit_turn(org.id, session.id, SubmitTurnRequest(content="go"))

    async with agent_stack.database.tenant_transaction(org.id) as tenant:
        keys = (
            (
                await tenant.session.execute(
                    text("SELECT DISTINCT idempotency_key FROM tool_execution_records")
                )
            )
            .scalars()
            .all()
        )
    assert len(keys) == 1  # one semantic call -> one P08 idempotency key, never iteration-tagged


async def test_max_tool_calls_per_turn_is_a_turn_wide_budget(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_write_tool(agent_stack, org.id, mock_http_server)
    # budget = 8; each iteration alone stays under the per-response bound.
    assert agent_stack.settings.agents.max_tool_calls_per_turn == 8
    agent = await agent_stack.service.create_agent(
        org.id,
        CreateAgentRequest(
            slug="budget-agent",
            display_name="Budget",
            model_profile_id=(
                await _provision(agent_stack, org.id, tool_keys=("crm.create",))
            ).model_profile_id,
            system_instructions="spend the budget",
            tool_keys=("crm.create",),
            max_tool_iterations=8,
        ),
    )
    hits = {"n": 0}

    def _handler(method: str, path: str, headers: Any, body: Any) -> tuple[int, dict[str, Any]]:
        hits["n"] += 1
        return 201, {"id": "c"}

    mock_http_server.set_handler(_handler)

    def _batch(prefix: str, count: int) -> FakeModelTurn:
        return FakeModelTurn(
            tool_calls=tuple(
                ("crm.create", {"path_params": {"id": f"{prefix}-{j}"}}) for j in range(count)
            ),
            tool_call_id_prefix=prefix,
        )

    agent_stack.script.append(_batch("i1", 4))  # turn total -> 4
    agent_stack.script.append(_batch("i2", 4))  # turn total -> 8 (exactly the ceiling)
    agent_stack.script.append(_batch("i3", 1))  # the 9th request must fail before execution
    agent_stack.script.append(FakeModelTurn(content="unreachable"))

    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    with pytest.raises(AgentToolLoopLimitError):
        await agent_stack.service.submit_turn(
            org.id, session.id, SubmitTurnRequest(content="spend")
        )

    assert hits["n"] == 8  # never exceeded the ceiling
    assert await _external_calls(agent_stack, org.id) == 8

    turns = await agent_stack.service.list_turns(org.id, session.id, limit=5)
    assert turns[0].state.value == "FAILED"
    assert turns[0].error_code == "NXS_AGENT_TOOL_LOOP_LIMIT"
    # the session survives a failed turn
    assert (await agent_stack.service.get_session(org.id, session.id)).state.value == "ACTIVE"


async def test_per_response_bound_still_rejects_one_oversized_response(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    from nexus_ai.agents.errors import AgentOutputInvalidError

    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_write_tool(agent_stack, org.id, mock_http_server)
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.create",))
    mock_http_server.set_handler(lambda m, p, h, b: (201, {"id": "c"}))

    limit = agent_stack.settings.agents.max_tool_calls_per_response
    agent_stack.script.append(
        FakeModelTurn(
            tool_calls=tuple(
                ("crm.create", {"path_params": {"id": str(j)}}) for j in range(limit + 1)
            )
        )
    )
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    with pytest.raises(AgentOutputInvalidError):
        await agent_stack.service.submit_turn(org.id, session.id, SubmitTurnRequest(content="go"))
    assert await _external_calls(agent_stack, org.id) == 0
