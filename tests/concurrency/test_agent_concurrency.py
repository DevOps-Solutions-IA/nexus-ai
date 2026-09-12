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


async def _model_permit_exists(stack: Any, org_id: Any, turn_id: Any, iteration: int) -> bool:
    from sqlalchemy import text

    async with stack.database.tenant_transaction(org_id) as tenant:
        count = (
            await tenant.session.execute(
                text(
                    "SELECT count(*) FROM ai_agent_model_dispatch_permits "
                    "WHERE turn_id = :t AND iteration = :i"
                ),
                {"t": str(turn_id), "i": iteration},
            )
        ).scalar_one()
        return bool(count > 0)


async def test_model_response_racing_a_cancellation_leaves_no_stale_answer(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    session = await _session(agent_stack, org.id, principal)
    # a generous hang: this test races a REAL cancellation against a REAL
    # asyncio.sleep, with no event-gated hook to make the ordering deterministic — a
    # short hang can finish for real before cancellation is even attempted on a heavily
    # loaded shared machine, so a wide margin (not a tight one) is what this wall-clock
    # race needs to stay reliable under full-suite load.
    agent_stack.script.append(FakeModelTurn(content="late answer", hang_seconds=10))

    turn = asyncio.create_task(
        agent_stack.service.submit_turn(org.id, session.id, SubmitTurnRequest(content="q"))
    )
    # the turn row reaching `state == RUNNING` (committed inside `_open_turn`) is NOT
    # sufficient proof that `submit_turn` has gone on to register the `_run_turn` task in
    # `self._turn_tasks` — an intervening `await self.get_agent(...)` sits between the
    # two. Cancelling in that window makes `_terminalize` find no task to cancel at all
    # (`self._turn_tasks.get(session_id)` returns `None`), so the model call is never
    # interrupted and legitimately completes — the durable model-dispatch permit for
    # iteration 0 (corrective #8/#9) can only exist once `_run_turn` itself is already
    # running, which is only possible after that task is registered — this is the
    # correct, race-free readiness signal, not the turn's own state column.
    turn_id = None
    deadline = asyncio.get_running_loop().time() + 5.0
    while True:
        turns = await agent_stack.service.list_turns(org.id, session.id, limit=1)
        if turns and turns[0].state.value == "RUNNING":
            turn_id = turns[0].id
            if await _model_permit_exists(agent_stack, org.id, turn_id, 0):
                break
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("timed out waiting for the model call to be authorized")
        await asyncio.sleep(0.01)
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
    # see test_model_response_racing_a_cancellation_leaves_no_stale_answer's identical
    # rationale: a fixed sleep is not sufficient proof that submit_turn has registered
    # the _run_turn task in self._turn_tasks (an intervening await self.get_agent(...)
    # sits between the turn row's own commit and that registration) — wait for the
    # durable model-dispatch permit instead, which can only exist once the task is
    # already registered.
    agent_stack.script.append(FakeModelTurn(content="answer", hang_seconds=10))

    turn = asyncio.create_task(
        agent_stack.service.submit_turn(org.id, session.id, SubmitTurnRequest(content="q"))
    )
    deadline = asyncio.get_running_loop().time() + 5.0
    while True:
        turns = await agent_stack.service.list_turns(org.id, session.id, limit=1)
        if (
            turns
            and turns[0].state.value == "RUNNING"
            and await _model_permit_exists(agent_stack, org.id, turns[0].id, 0)
        ):
            break
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("timed out waiting for the model call to be authorized")
        await asyncio.sleep(0.01)
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
