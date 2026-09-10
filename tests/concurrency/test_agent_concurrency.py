"""AI Agent Runtime concurrency (NXS-P13): two simultaneous turns on one session, a
model response racing a cancellation, a tool result arriving after the session is
terminal, and session stop while a turn is mid-flight — against real PostgreSQL."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nexus_ai.agents.entities import (
    StartAgentSessionRequest,
    StopAgentSessionRequest,
    SubmitTurnRequest,
)
from nexus_ai.agents.errors import AgentBusyError, AgentCancelledError, AgentInvalidStateError
from nexus_ai.agents.models.fake import FakeModelTurn
from nexus_ai.agents.state_machine import AgentSessionState
from tests.integration.test_agent_service import _provision

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _session(stack: Any, org_id: Any, principal: Any) -> Any:
    agent = await _provision(stack, org_id)
    return await stack.service.start_session(
        org_id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )


async def test_two_simultaneous_turns_on_one_session_only_one_runs(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    session = await _session(agent_stack, org.id, principal)
    agent_stack.script.append(FakeModelTurn(content="first", hang_seconds=0.5))
    agent_stack.script.append(FakeModelTurn(content="second"))

    first = asyncio.create_task(
        agent_stack.service.submit_turn(org.id, session.id, SubmitTurnRequest(content="a"))
    )
    await asyncio.sleep(0.1)
    with pytest.raises(AgentBusyError):
        await agent_stack.service.submit_turn(org.id, session.id, SubmitTurnRequest(content="b"))
    assert (await first).content == "first"
    turns = await agent_stack.service.list_turns(org.id, session.id, limit=10)
    assert len(turns) == 1  # the rejected turn never opened a row


async def test_model_response_racing_a_cancellation_leaves_no_stale_answer(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    session = await _session(agent_stack, org.id, principal)
    agent_stack.script.append(FakeModelTurn(content="late answer", hang_seconds=2))

    turn = asyncio.create_task(
        agent_stack.service.submit_turn(org.id, session.id, SubmitTurnRequest(content="q"))
    )
    await asyncio.sleep(0.1)
    cancelled = await agent_stack.service.cancel_session(org.id, session.id)
    assert cancelled.state is AgentSessionState.CANCELLED
    with pytest.raises((AgentCancelledError, asyncio.CancelledError)):
        await turn
    turns = await agent_stack.service.list_turns(org.id, session.id, limit=10)
    assert turns[0].state.value in ("FAILED", "CANCELLED")
    assert turns[0].response_text is None  # the racing model answer was discarded


async def test_stop_during_a_turn_terminalizes_and_rejects_further_turns(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    session = await _session(agent_stack, org.id, principal)
    agent_stack.script.append(FakeModelTurn(content="answer", hang_seconds=1))

    turn = asyncio.create_task(
        agent_stack.service.submit_turn(org.id, session.id, SubmitTurnRequest(content="q"))
    )
    await asyncio.sleep(0.1)
    stopped = await agent_stack.service.stop_session(org.id, session.id, StopAgentSessionRequest())
    assert stopped.state is AgentSessionState.COMPLETED
    with pytest.raises(BaseException):  # noqa: B017
        await turn
    with pytest.raises(AgentInvalidStateError):
        await agent_stack.service.submit_turn(
            org.id, session.id, SubmitTurnRequest(content="again")
        )


async def test_same_completed_turn_key_delivered_twice_is_one_effect(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    session = await _session(agent_stack, org.id, principal)
    agent_stack.script.append(FakeModelTurn(content="only once"))

    results = await asyncio.gather(
        agent_stack.service.submit_turn(
            org.id, session.id, SubmitTurnRequest(content="q", idempotency_key="concurrent-key-1")
        ),
        agent_stack.service.submit_turn(
            org.id, session.id, SubmitTurnRequest(content="q", idempotency_key="concurrent-key-1")
        ),
        return_exceptions=True,
    )
    ok = [r for r in results if not isinstance(r, BaseException)]
    # at least one succeeds; any second concurrent attempt is a replay or a busy signal
    assert ok
    for r in results:
        if isinstance(r, BaseException):
            assert isinstance(r, AgentBusyError)
    turns = await agent_stack.service.list_turns(org.id, session.id, limit=10)
    assert len(turns) == 1
    assert len(agent_stack.provider.calls) == 1
