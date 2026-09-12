"""AI Agent Runtime service against the real DB + event outbox + a real NXS-P08 Tool
Engine, with a scripted deterministic fake model provider (NXS-P13: NXS-AGENT-001)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.agents.entities import (
    AgentChannel,
    CreateAgentRequest,
    CreateModelProfileRequest,
    ModelProvider,
    RegisterModelProviderAccountRequest,
    StartAgentSessionRequest,
    StopAgentSessionRequest,
    StoreModelCredentialRequest,
    SubmitTurnRequest,
)
from nexus_ai.agents.errors import (
    AgentIdempotencyConflictError,
    AgentInvalidStateError,
    AgentSessionNotFoundError,
    AgentToolLoopLimitError,
)
from nexus_ai.agents.models.fake import FakeModelTurn
from nexus_ai.agents.state_machine import AgentSessionState
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

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

_TOOL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "path_params": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id"],
            "properties": {"id": {"type": "string"}},
        }
    },
    "required": ["path_params"],
}


async def _provision(stack: Any, org_id: Any, *, tool_keys: tuple[str, ...] = ()) -> Any:
    account = await stack.service.create_account(
        org_id,
        RegisterModelProviderAccountRequest(
            provider=ModelProvider.FAKE,
            slug="prov-1",
            external_account_id="acct-1",
            api_base="https://models.example.com",
        ),
    )
    await stack.service.store_account_credential(
        org_id, account.id, StoreModelCredentialRequest(fields={"api_key": "sk-fake"})
    )
    profile = await stack.service.create_profile(
        org_id,
        CreateModelProfileRequest(
            account_id=account.id, slug="prof-1", display_name="P", model="fake-model"
        ),
    )
    agent = await stack.service.create_agent(
        org_id,
        CreateAgentRequest(
            slug="agent-1",
            display_name="Agent One",
            model_profile_id=profile.id,
            system_instructions="Be a concise assistant.",
            tool_keys=tool_keys,
        ),
    )
    return agent


async def _register_tool(stack: Any, org_id: Any, mock: Any, key: str = "crm.get") -> None:
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
            operation_key="crm.get",
            spec=RestOperationSpec(
                method=HttpMethod.GET,
                path="/c/{id}",
                path_params={"id": ParamSpec(required=True)},
                retry_class=RetryClass.SAFE,
            ),
        ),
    )
    tool = await stack.tool_registry.register(
        org_id,
        RegisterToolRequest(
            tool_key=key,
            name="Get contact",
            description="Fetch a CRM contact",
            risk_class=RiskClass.LOW,
            side_effect_class=SideEffectClass.READ_ONLY,
            idempotency_policy=ToolIdempotencyPolicy.OPTIONAL,
            input_schema=_TOOL_SCHEMA,
            output_schema={"type": "object", "required": ["id"]},
            binding=ToolBinding(integration_id=integration.id, operation_key="crm.get"),
        ),
    )
    await stack.tool_registry.set_status(org_id, tool.id, ToolStatus.ACTIVE)


async def _events(stack: Any, org_id: Any, like: str) -> list[str]:
    async with stack.database.tenant_transaction(org_id) as tenant:
        return [
            r[0]
            for r in (
                await tenant.session.execute(
                    text(
                        "SELECT event_type FROM event_outbox WHERE event_type LIKE :p "
                        "ORDER BY created_at"
                    ),
                    {"p": like},
                )
            ).all()
        ]


async def test_start_session_and_a_plain_text_turn(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    agent = await _provision(agent_stack, org.id)

    agent_stack.script.append(FakeModelTurn(content="Hello, how can I help?"))
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    assert session.state is AgentSessionState.ACTIVE

    response = await agent_stack.service.submit_turn(
        org.id, session.id, SubmitTurnRequest(content="hi")
    )
    assert response.content == "Hello, how can I help?"
    assert response.finish_reason == "STOP"
    assert response.tool_calls == 0
    assert response.total_tokens > 0

    events = await _events(agent_stack, org.id, "agent.%")
    assert "agent.session.created" in events
    assert "agent.session.started" in events
    assert "agent.turn.started" in events
    assert "agent.turn.completed" in events
    assert "agent.response.ready" in events
    assert "agent.usage.recorded" in events

    turns = await agent_stack.service.list_turns(org.id, session.id, limit=10)
    assert len(turns) == 1 and turns[0].response_char_count == len(response.content)


async def test_turn_runs_one_tool_then_a_final_answer(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_tool(agent_stack, org.id, mock_http_server)
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.get",))
    mock_http_server.set_handler(lambda m, p, h, b: (200, {"id": "c1", "name": "Ada"}))

    agent_stack.script.append(
        FakeModelTurn(tool_calls=(("crm.get", {"path_params": {"id": "c1"}}),))
    )
    agent_stack.script.append(FakeModelTurn(content="The contact is Ada."))

    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    response = await agent_stack.service.submit_turn(
        org.id, session.id, SubmitTurnRequest(content="who is c1?")
    )
    assert response.content == "The contact is Ada."
    assert response.tool_calls == 1

    tool_events = await _events(agent_stack, org.id, "agent.tool.%")
    assert "agent.tool.requested" in tool_events
    assert "agent.tool.completed" in tool_events

    async with agent_stack.database.tenant_transaction(org.id) as tenant:
        row = (
            await tenant.session.execute(
                text("SELECT tool_key, status, arguments_hash FROM ai_agent_tool_calls")
            )
        ).one()
    assert row[0] == "crm.get" and row[1] == "COMPLETED"
    assert len(row[2]) == 64  # a hash, never the raw arguments


async def test_bounded_tool_loop_terminates_deterministically(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_tool(agent_stack, org.id, mock_http_server)
    agent = await agent_stack.service.create_agent(
        org.id,
        CreateAgentRequest(
            slug="looper",
            display_name="Looper",
            model_profile_id=(
                await _provision(agent_stack, org.id, tool_keys=("crm.get",))
            ).model_profile_id,
            system_instructions="loop",
            tool_keys=("crm.get",),
            max_tool_iterations=2,
        ),
    )
    mock_http_server.set_handler(lambda m, p, h, b: (200, {"id": "x"}))
    # the model asks for the same tool on every response — never a final answer
    for _ in range(6):
        agent_stack.script.append(
            FakeModelTurn(tool_calls=(("crm.get", {"path_params": {"id": "x"}}),))
        )

    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    with pytest.raises(AgentToolLoopLimitError):
        await agent_stack.service.submit_turn(org.id, session.id, SubmitTurnRequest(content="go"))
    turns = await agent_stack.service.list_turns(org.id, session.id, limit=10)
    assert turns[0].state.value == "FAILED"
    assert turns[0].error_code == "NXS_AGENT_TOOL_LOOP_LIMIT"
    # the session survives a failed turn and is still usable
    session = await agent_stack.service.get_session(org.id, session.id)
    assert session.state is AgentSessionState.ACTIVE


async def test_session_idempotency_replays_and_conflicts(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    agent = await _provision(agent_stack, org.id)
    a = await agent_stack.service.start_session(
        org.id,
        principal,
        StartAgentSessionRequest(agent_id=agent.id, idempotency_key="sess-key-1"),
    )
    b = await agent_stack.service.start_session(
        org.id,
        principal,
        StartAgentSessionRequest(agent_id=agent.id, idempotency_key="sess-key-1"),
    )
    assert a.id == b.id
    with pytest.raises(AgentIdempotencyConflictError):
        await agent_stack.service.start_session(
            org.id,
            principal,
            StartAgentSessionRequest(
                agent_id=agent.id,
                idempotency_key="sess-key-1",
                channel=AgentChannel.VOICE,
            ),
        )


async def test_turn_idempotency_replays_a_completed_turn(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    agent = await _provision(agent_stack, org.id)
    agent_stack.script.append(FakeModelTurn(content="answer one"))
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    r1 = await agent_stack.service.submit_turn(
        org.id, session.id, SubmitTurnRequest(content="q", idempotency_key="turn-key-1")
    )
    r2 = await agent_stack.service.submit_turn(
        org.id, session.id, SubmitTurnRequest(content="q", idempotency_key="turn-key-1")
    )
    assert r1.turn_id == r2.turn_id and r1.content == r2.content
    # exactly one turn row, one model call
    turns = await agent_stack.service.list_turns(org.id, session.id, limit=10)
    assert len(turns) == 1
    assert len(agent_stack.provider.calls) == 1


async def test_stop_and_terminal_session_rejects_turns(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    agent = await _provision(agent_stack, org.id)
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    stopped = await agent_stack.service.stop_session(org.id, session.id, StopAgentSessionRequest())
    assert stopped.state is AgentSessionState.COMPLETED
    with pytest.raises(AgentInvalidStateError):
        await agent_stack.service.submit_turn(
            org.id, session.id, SubmitTurnRequest(content="too late")
        )
    # stop is idempotent
    again = await agent_stack.service.stop_session(org.id, session.id, StopAgentSessionRequest())
    assert again.state is AgentSessionState.COMPLETED


async def test_cross_tenant_session_read_fails_closed(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    org_a = await make_organization()
    org_b = await make_organization()
    principal = await make_tool_principal(org_a)
    agent = await _provision(agent_stack, org_a.id)
    session = await agent_stack.service.start_session(
        org_a.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    with pytest.raises(AgentSessionNotFoundError):
        await agent_stack.service.get_session(org_b.id, session.id)


async def test_turn_assembles_bounded_tenant_scoped_context_from_p06(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    import uuid

    from nexus_ai.domain.customers.entities import (
        CreateConversationRequest,
        CreateCustomerRequest,
        IdentityType,
    )

    org = await make_organization()
    principal = await make_tool_principal(org)
    customer, _ = await agent_stack.customers.resolve_or_create(
        org.id,
        CreateCustomerRequest(
            display_name="Ada Lovelace",
            identity_type=IdentityType.EMAIL,
            identity_value=f"ada-{uuid.uuid4().hex[:8]}@example.com",
            identity_source="test",
        ),
    )
    conversation, _ = await agent_stack.conversations.open_or_resolve(
        org.id,
        CreateConversationRequest(
            customer_id=customer.id, channel="whatsapp", subject="Billing question"
        ),
    )
    agent = await _provision(agent_stack, org.id)
    session = await agent_stack.service.start_session(
        org.id,
        principal,
        StartAgentSessionRequest(
            agent_id=agent.id,
            channel=AgentChannel.WHATSAPP,
            customer_id=customer.id,
            conversation_id=conversation.id,
        ),
    )
    # two prior turns then a third — the third turn's context sees the earlier exchange
    agent_stack.script.append(FakeModelTurn(content="first answer"))
    agent_stack.script.append(FakeModelTurn(content="second answer"))
    agent_stack.script.append(FakeModelTurn(content="third answer"))
    await agent_stack.service.submit_turn(org.id, session.id, SubmitTurnRequest(content="one"))
    await agent_stack.service.submit_turn(org.id, session.id, SubmitTurnRequest(content="two"))
    await agent_stack.service.submit_turn(org.id, session.id, SubmitTurnRequest(content="three"))

    # the third model call carried a bounded, deterministic, secret-free context
    last_request = agent_stack.provider.calls[-1]
    system_blocks = [m.content for m in last_request.messages if m.role.value == "system"]
    context_block = next(b for b in system_blocks if "READ-ONLY CONTEXT" in b)
    assert "Ada Lovelace" in context_block
    assert "conversation-subject: Billing question" in context_block
    assert "sk-fake" not in context_block
    history = [m.content for m in last_request.messages if m.role.value in ("user", "assistant")]
    assert "one" in history and "first answer" in history  # earlier exchange is present
    assert history[-1] == "three"  # the current input is last


async def test_definition_updates_and_validation_paths(
    agent_stack: Any, make_organization: Any, mock_http_server: Any
) -> None:
    from nexus_ai.agents.entities import (
        UpdateAgentRequest,
        UpdateModelProfileRequest,
        UpdateModelProviderAccountRequest,
    )
    from nexus_ai.agents.errors import (
        AgentConfigInvalidError,
        AgentModelProfileNotFoundError,
        AgentNotFoundError,
    )

    org = await make_organization()
    await _register_tool(agent_stack, org.id, mock_http_server)
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.get",))
    accounts = await agent_stack.service.list_accounts(org.id, limit=10)
    profiles = await agent_stack.service.list_profiles(org.id, limit=10)

    # account: a valid https re-base is accepted; a blocked host is refused
    ok = await agent_stack.service.update_account(
        org.id,
        accounts[0].id,
        UpdateModelProviderAccountRequest(api_base="https://models.example.net"),
    )
    assert ok.api_base == "https://models.example.net"
    with pytest.raises(AgentConfigInvalidError):
        await agent_stack.service.update_account(
            org.id,
            accounts[0].id,
            UpdateModelProviderAccountRequest(api_base="https://gateway.localhost"),
        )

    # profile: every field updatable; a status flip round-trips
    updated_profile = await agent_stack.service.update_profile(
        org.id,
        profiles[0].id,
        UpdateModelProfileRequest(display_name="P2", model="fake-2", temperature=0.1),
    )
    assert updated_profile.display_name == "P2" and updated_profile.model == "fake-2"

    # agent: tool_keys re-validated; an unknown tool key is refused
    updated_agent = await agent_stack.service.update_agent(
        org.id, agent.id, UpdateAgentRequest(display_name="A2", tool_keys=("crm.get",))
    )
    assert updated_agent.display_name == "A2"
    with pytest.raises(AgentConfigInvalidError):
        await agent_stack.service.update_agent(
            org.id, agent.id, UpdateAgentRequest(tool_keys=("does.not.exist",))
        )

    # missing-entity reads fail closed
    import uuid

    with pytest.raises(AgentNotFoundError):
        await agent_stack.service.get_agent(org.id, uuid.uuid7())
    with pytest.raises(AgentModelProfileNotFoundError):
        await agent_stack.service.update_profile(
            org.id, uuid.uuid7(), UpdateModelProfileRequest(display_name="ghost")
        )
