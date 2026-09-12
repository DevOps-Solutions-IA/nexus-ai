"""NXS-P13 audit corrective #4 — one idempotency key == one immutable logical turn ==
one model execution owner, enforced at the DATABASE boundary across independent workers.
Plus the truthful stale-terminal error code.

Fail-first against 3ecc221: _open_turn returned a RUNNING turn matched by idempotency key
to a second worker, and submit_turn only short-circuited on COMPLETED — so two replicas
could execute the same logical AgentTurn simultaneously. And _discard_stale_turn labelled
a normal stop() as NXS_AGENT_SESSION_EXPIRED.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.agents.entities import (
    StartAgentSessionRequest,
    StopAgentSessionRequest,
    SubmitTurnRequest,
)
from nexus_ai.agents.errors import (
    AgentBusyError,
    AgentIdempotencyConflictError,
    AgentIdempotentReplayError,
)
from nexus_ai.agents.models.fake import FakeModelTurn
from tests.concurrency.test_agent_lifecycle_race import _compress, _second_service
from tests.integration.test_agent_service import _provision, _register_tool

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

_KEY = "idem-key-corrective-4"


async def _counts(stack: Any, org_id: Any, session_id: Any) -> dict[str, Any]:
    async with stack.database.tenant_transaction(org_id) as tenant:
        turns = (
            await tenant.session.execute(
                text(
                    "SELECT count(*), coalesce(sum(input_tokens),0), "
                    "coalesce(sum(output_tokens),0) "
                    "FROM ai_agent_turns WHERE session_id = :s"
                ),
                {"s": str(session_id)},
            )
        ).one()
        sess = (
            await tenant.session.execute(
                text(
                    "SELECT turn_count, input_tokens, output_tokens, tool_call_count "
                    "FROM ai_agent_sessions WHERE id = :s"
                ),
                {"s": str(session_id)},
            )
        ).one()
        started = (
            await tenant.session.execute(
                text(
                    "SELECT count(*) FROM event_outbox "
                    "WHERE event_type = 'agent.turn.started' "
                    "AND envelope::text LIKE :s"
                ),
                {"s": f"%{session_id}%"},
            )
        ).scalar_one()
        execs = (
            await tenant.session.execute(text("SELECT count(*) FROM tool_execution_records"))
        ).scalar_one()
    return {
        "turn_rows": turns[0],
        "turn_input_tokens": int(turns[1]),
        "turn_output_tokens": int(turns[2]),
        "session_turn_count": sess[0],
        "session_input_tokens": sess[1],
        "session_output_tokens": sess[2],
        "session_tool_call_count": sess[3],
        "turn_started_events": started,
        "tool_execs": execs,
    }


async def test_two_workers_one_key_one_model_execution(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_tool(agent_stack, org.id, mock_http_server)
    mock_http_server.set_handler(lambda m, p, h, b: (200, {"id": "x"}))

    worker_a = agent_stack.service
    worker_b, provider_b = _second_service(agent_stack)
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.get",))
    session = await worker_a.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )

    body = SubmitTurnRequest(content="do the thing", idempotency_key=_KEY)

    # Worker A claims the turn and blocks in the model.
    agent_stack.script.append(FakeModelTurn(content="the one true answer", hang_seconds=2.0))
    task_a = asyncio.create_task(worker_a.submit_turn(org.id, session.id, body))
    await asyncio.sleep(0.3)

    # Worker B submits the SAME key + content while A is RUNNING.
    provider_b.script.append(FakeModelTurn(content="B must never run"))
    with pytest.raises(AgentBusyError):
        await worker_b.submit_turn(org.id, session.id, body)

    assert provider_b.calls == []  # B never entered _run_turn
    mid = await _counts(agent_stack, org.id, session.id)
    assert mid["turn_rows"] == 1  # one logical AgentTurn
    assert mid["turn_started_events"] == 1  # one agent.turn.started

    response_a = await task_a
    assert response_a.content == "the one true answer"

    after = await _counts(agent_stack, org.id, session.id)
    assert after["turn_rows"] == 1
    assert after["turn_started_events"] == 1
    assert after["session_turn_count"] == 1
    assert after["tool_execs"] == 0  # no tool call this turn, and definitely not doubled
    a_in, a_out = after["session_input_tokens"], after["session_output_tokens"]

    # a later replay with the same key returns the persisted completed result — no rerun.
    replay = await worker_b.submit_turn(org.id, session.id, body)
    assert replay.turn_id == response_a.turn_id
    assert replay.content == "the one true answer"
    assert provider_b.calls == []  # still nothing

    final = await _counts(agent_stack, org.id, session.id)
    assert final["turn_rows"] == 1
    assert final["session_turn_count"] == 1
    assert (final["session_input_tokens"], final["session_output_tokens"]) == (a_in, a_out)
    assert final["session_tool_call_count"] == after["session_tool_call_count"]  # not doubled

    await worker_b.shutdown()


async def test_same_key_different_content_conflicts(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    agent = await _provision(agent_stack, org.id)
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    agent_stack.script.append(FakeModelTurn(content="first"))
    await agent_stack.service.submit_turn(
        org.id, session.id, SubmitTurnRequest(content="alpha", idempotency_key=_KEY)
    )
    with pytest.raises(AgentIdempotencyConflictError):
        await agent_stack.service.submit_turn(
            org.id, session.id, SubmitTurnRequest(content="BETA", idempotency_key=_KEY)
        )


@pytest.mark.parametrize("terminal", ["FAILED", "CANCELLED"])
async def test_same_key_after_a_terminal_turn_never_reruns(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, terminal: str
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    agent = await _provision(agent_stack, org.id)
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    body = SubmitTurnRequest(content="q", idempotency_key=_KEY)

    if terminal == "FAILED":
        _compress(agent_stack.service, turn_deadline_seconds=0.4)
        agent_stack.script.append(FakeModelTurn(content="slow", hang_seconds=3.0))
        with pytest.raises(Exception):  # noqa: B017 - AgentTurnTimeoutError
            await agent_stack.service.submit_turn(org.id, session.id, body)
        _compress(agent_stack.service, turn_deadline_seconds=120.0)
    else:  # CANCELLED — an in-flight task cancellation (e.g. graceful shutdown) that
        # leaves the SESSION live but the turn CANCELLED.
        agent_stack.script.append(FakeModelTurn(content="slow", hang_seconds=3.0))
        task = asyncio.create_task(agent_stack.service.submit_turn(org.id, session.id, body))
        await asyncio.sleep(0.2)
        agent_stack.service._turn_tasks[session.id].cancel()
        with pytest.raises(BaseException):  # noqa: B017 - CancelledError
            await task

    calls_before = len(agent_stack.provider.calls)
    turns = await agent_stack.service.list_turns(org.id, session.id, limit=5)
    assert turns[0].state.value in ("FAILED", "CANCELLED")
    # the session itself is still usable (only the turn is terminal)
    assert (await agent_stack.service.get_session(org.id, session.id)).state.value == "ACTIVE"

    # re-submitting the SAME key does NOT start a fresh execution
    agent_stack.script.append(FakeModelTurn(content="MUST NOT RUN"))
    with pytest.raises(AgentIdempotentReplayError) as excinfo:
        await agent_stack.service.submit_turn(org.id, session.id, body)
    assert excinfo.value.extensions["turn_state"] in ("FAILED", "CANCELLED")
    assert "original_error_code" in excinfo.value.extensions
    assert len(agent_stack.provider.calls) == calls_before  # no new model call


async def test_completed_replay_is_deterministic_single_service(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    agent = await _provision(agent_stack, org.id)
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    body = SubmitTurnRequest(content="q", idempotency_key=_KEY)
    agent_stack.script.append(FakeModelTurn(content="answer one"))
    r1 = await agent_stack.service.submit_turn(org.id, session.id, body)
    r2 = await agent_stack.service.submit_turn(org.id, session.id, body)
    assert r1.turn_id == r2.turn_id and r1.content == r2.content == "answer one"
    assert len(agent_stack.provider.calls) == 1
    turns = await agent_stack.service.list_turns(org.id, session.id, limit=5)
    assert len(turns) == 1


async def test_stop_session_racing_a_turn_is_not_labelled_expired(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    """Audit corrective #4 secondary: a concurrent stop()/COMPLETED must NOT produce a
    fabricated NXS_AGENT_SESSION_EXPIRED on the discarded turn or its event."""
    org = await make_organization()
    principal = await make_tool_principal(org)
    worker_a = agent_stack.service
    worker_b, _pb = _second_service(agent_stack)
    agent = await _provision(agent_stack, org.id)
    session = await worker_a.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    agent_stack.script.append(FakeModelTurn(content="A answer", hang_seconds=1.2))
    task_a = asyncio.create_task(
        worker_a.submit_turn(org.id, session.id, SubmitTurnRequest(content="a"))
    )
    await asyncio.sleep(0.2)
    stopped = await worker_b.stop_session(org.id, session.id, StopAgentSessionRequest())
    assert stopped.state.value == "COMPLETED"

    with pytest.raises(Exception):  # noqa: B017
        await task_a

    async with agent_stack.database.tenant_transaction(org.id) as tenant:
        turn_code = (
            await tenant.session.execute(
                text("SELECT error_code FROM ai_agent_turns WHERE session_id = :s"),
                {"s": str(session.id)},
            )
        ).scalar_one()
        events = (
            await tenant.session.execute(
                text(
                    "SELECT envelope::text FROM event_outbox WHERE event_type = 'agent.turn.failed'"
                )
            )
        ).all()
    assert turn_code != "NXS_AGENT_SESSION_EXPIRED"
    assert turn_code == "NXS_AGENT_INVALID_STATE"  # truthful: a normal stop, not expiry
    assert events and all("NXS_AGENT_SESSION_EXPIRED" not in row[0] for row in events)

    await worker_b.shutdown()
