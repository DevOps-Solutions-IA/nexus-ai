"""AI Agent Runtime resilience (NXS-P13): model unavailable / timeout / malformed, tool
failure / denial, no final answer, shutdown with an active session, duplicate delivery."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nexus_ai.agents.entities import (
    StartAgentSessionRequest,
    StopAgentSessionRequest,
    SubmitTurnRequest,
)
from nexus_ai.agents.errors import (
    AgentOutputInvalidError,
    AgentProviderError,
    AgentProviderTimeoutError,
)
from nexus_ai.agents.models.fake import FakeModelTurn
from nexus_ai.agents.state_machine import AgentSessionState
from tests.integration.test_agent_service import _provision, _register_tool

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _live(stack: Any, org_id: Any, principal: Any, **kw: Any) -> Any:
    agent = await _provision(stack, org_id, tool_keys=kw.get("tools", ()))
    return await stack.service.start_session(
        org_id, principal, StartAgentSessionRequest(agent_id=agent.id)
    ), agent


@pytest.mark.parametrize(
    ("turn", "error"),
    [
        (FakeModelTurn(raise_connect=True), AgentProviderError),
        (FakeModelTurn(raise_timeout=True), AgentProviderTimeoutError),
        (FakeModelTurn(raise_status=503), AgentProviderError),
        (FakeModelTurn(unknown_finish=True), AgentOutputInvalidError),
    ],
)
async def test_model_failure_modes_map_to_stable_errors_and_fail_the_turn(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, turn: Any, error: type
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    session, _ = await _live(agent_stack, org.id, principal)
    agent_stack.script.append(turn)
    with pytest.raises(error):
        await agent_stack.service.submit_turn(org.id, session.id, SubmitTurnRequest(content="q"))
    turns = await agent_stack.service.list_turns(org.id, session.id, limit=5)
    assert turns[0].state.value == "FAILED" and turns[0].error_code is not None
    # the session survives and stays usable
    assert (
        await agent_stack.service.get_session(org.id, session.id)
    ).state is AgentSessionState.ACTIVE


async def test_a_failing_tool_is_reported_not_fatal(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_tool(agent_stack, org.id, mock_http_server)
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.get",))
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    mock_http_server.set_handler(lambda m, p, h, b: (500, {"error": "boom"}))
    agent_stack.script.append(
        FakeModelTurn(tool_calls=(("crm.get", {"path_params": {"id": "1"}}),))
    )
    agent_stack.script.append(FakeModelTurn(content="the tool failed, I could not help"))
    response = await agent_stack.service.submit_turn(
        org.id, session.id, SubmitTurnRequest(content="q")
    )
    assert "could not" in response.content
    turns = await agent_stack.service.list_turns(org.id, session.id, limit=5)
    assert turns[0].state.value == "COMPLETED"  # the turn recovered from a tool error


async def test_provider_returns_no_final_answer_hits_the_loop_ceiling(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_tool(agent_stack, org.id, mock_http_server)
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.get",))
    mock_http_server.set_handler(lambda m, p, h, b: (200, {"id": "x"}))
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    for _ in range(agent_stack.settings.agents.max_tool_iterations + 2):
        agent_stack.script.append(
            FakeModelTurn(tool_calls=(("crm.get", {"path_params": {"id": "x"}}),))
        )
    from nexus_ai.agents.errors import AgentToolLoopLimitError

    with pytest.raises(AgentToolLoopLimitError):
        await agent_stack.service.submit_turn(org.id, session.id, SubmitTurnRequest(content="q"))


async def test_shutdown_cancels_an_in_flight_turn(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    session, _ = await _live(agent_stack, org.id, principal)
    agent_stack.script.append(FakeModelTurn(content="slow", hang_seconds=30))
    task = asyncio.create_task(
        agent_stack.service.submit_turn(org.id, session.id, SubmitTurnRequest(content="q"))
    )
    await asyncio.sleep(0.05)
    await agent_stack.service.shutdown()
    with pytest.raises(BaseException):  # noqa: B017 - CancelledError / AgentCancelledError
        await task
    # no leaked task
    assert not agent_stack.service._turn_tasks


async def test_repeated_lifecycle_no_task_leak(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    agent = await _provision(agent_stack, org.id)
    for i in range(4):
        session = await agent_stack.service.start_session(
            org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
        )
        agent_stack.script.append(FakeModelTurn(content=f"answer {i}"))
        await agent_stack.service.submit_turn(org.id, session.id, SubmitTurnRequest(content="q"))
        await agent_stack.service.stop_session(org.id, session.id, StopAgentSessionRequest())
    assert not agent_stack.service._turn_tasks
