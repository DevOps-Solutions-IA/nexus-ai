"""NXS-P13 audit corrective #5 — response-EXACT idempotent replay, and historical replay
ordering that survives the parent session becoming terminal.

Fail-first against audited HEAD e736133:
  * `_response_from_turn` used `turn.tool_iterations` (a DIFFERENT number from the
    original response's `len(outcome.tool_calls)` whenever one model response requests
    more than one tool) and hardcoded `correlation_id=None` — a replay could return a
    payload that differs from the original `AgentResponse`.
  * `_open_turn` checked the SESSION's current terminal / lifetime state BEFORE looking
    up the idempotency key, so a historical COMPLETED/FAILED/CANCELLED turn became
    unreachable once the session itself later terminalised — violating "one idempotency
    key names one immutable logical turn".

Locks INV-IDEM-001/002/005/006/007/008/009/010 with real PostgreSQL.
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
    AgentIdempotencyConflictError,
    AgentIdempotentReplayError,
    AgentInvalidStateError,
    AgentSessionExpiredError,
)
from nexus_ai.agents.models.fake import FakeModelTurn
from tests.concurrency.test_agent_lifecycle_race import _compress
from tests.integration.test_agent_service import _provision, _register_tool

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _snapshot(stack: Any, org_id: Any, session_id: Any) -> dict[str, Any]:
    async with stack.database.tenant_transaction(org_id) as tenant:
        sess = (
            await tenant.session.execute(
                text(
                    "SELECT turn_count, input_tokens, output_tokens, tool_call_count "
                    "FROM ai_agent_sessions WHERE id = :s"
                ),
                {"s": str(session_id)},
            )
        ).one()
        turn_rows = (
            await tenant.session.execute(
                text("SELECT count(*) FROM ai_agent_turns WHERE session_id = :s"),
                {"s": str(session_id)},
            )
        ).scalar_one()
        tool_rows = (
            await tenant.session.execute(text("SELECT count(*) FROM ai_agent_tool_calls"))
        ).scalar_one()
        tool_execs = (
            await tenant.session.execute(text("SELECT count(*) FROM tool_execution_records"))
        ).scalar_one()
        events = (
            await tenant.session.execute(
                text("SELECT event_type FROM event_outbox WHERE event_type LIKE 'agent.%'")
            )
        ).all()
    return {
        "turn_count": sess[0],
        "input_tokens": sess[1],
        "output_tokens": sess[2],
        "tool_call_count": sess[3],
        "turn_rows": turn_rows,
        "tool_call_rows": tool_rows,
        "tool_execs": tool_execs,
        "events": sorted(r[0] for r in events),
    }


# ---------------------------------------------------------------- TEST 1


async def test_multi_tool_response_is_replayed_field_exact(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_tool(agent_stack, org.id, mock_http_server)
    mock_http_server.set_handler(lambda m, p, h, b: (200, {"id": "x"}))
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.get",))
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )

    # ONE model response requesting THREE distinct tool calls, then the final answer.
    agent_stack.script.append(
        FakeModelTurn(
            tool_calls=(
                ("crm.get", {"path_params": {"id": "a"}}),
                ("crm.get", {"path_params": {"id": "b"}}),
                ("crm.get", {"path_params": {"id": "c"}}),
            )
        )
    )
    agent_stack.script.append(FakeModelTurn(content="three contacts fetched"))

    body = SubmitTurnRequest(
        content="fetch a, b, c", idempotency_key="fidelity-key-1", correlation_id="corr-abc"
    )
    original = await agent_stack.service.submit_turn(org.id, session.id, body)
    assert original.tool_calls == 3  # not 1 (tool_iterations) — three distinct executions
    assert original.correlation_id == "corr-abc"

    replay = await agent_stack.service.submit_turn(org.id, session.id, body)
    assert replay.model_dump() == original.model_dump()
    assert replay.tool_calls == 3
    assert replay.correlation_id == "corr-abc"

    # only 3 tool executions total — the replay ran neither the model nor any tool again.
    async with agent_stack.database.tenant_transaction(org.id) as tenant:
        execs = (
            await tenant.session.execute(text("SELECT count(*) FROM tool_execution_records"))
        ).scalar_one()
    assert execs == 3
    assert len(agent_stack.provider.calls) == 2  # the tool-call response + the final answer


# ---------------------------------------------------------------- TEST 2 / 3 / 4


async def test_completed_replay_after_stop_session(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    agent = await _provision(agent_stack, org.id)
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    body = SubmitTurnRequest(content="q", idempotency_key="key-stop", correlation_id="c-stop")
    agent_stack.script.append(FakeModelTurn(content="answer"))
    original = await agent_stack.service.submit_turn(org.id, session.id, body)

    stopped = await agent_stack.service.stop_session(org.id, session.id, StopAgentSessionRequest())
    assert stopped.state.value == "COMPLETED"

    replay = await agent_stack.service.submit_turn(org.id, session.id, body)
    assert replay.model_dump() == original.model_dump()


async def test_completed_replay_after_cancel_session(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    agent = await _provision(agent_stack, org.id)
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    body = SubmitTurnRequest(content="q", idempotency_key="k-cancel", correlation_id="c-cancel")
    agent_stack.script.append(FakeModelTurn(content="answer"))
    original = await agent_stack.service.submit_turn(org.id, session.id, body)

    cancelled = await agent_stack.service.cancel_session(org.id, session.id)
    assert cancelled.state.value == "CANCELLED"

    replay = await agent_stack.service.submit_turn(org.id, session.id, body)
    assert replay.model_dump() == original.model_dump()


async def test_completed_replay_after_session_expires(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    _compress(agent_stack.service, max_session_seconds=1.0)
    agent = await _provision(agent_stack, org.id)
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    body = SubmitTurnRequest(content="q", idempotency_key="k-expire", correlation_id="c-expire")
    agent_stack.script.append(FakeModelTurn(content="answer"))
    original = await agent_stack.service.submit_turn(org.id, session.id, body)

    await asyncio.sleep(1.1)
    # drive the session to an ACTUAL EXPIRED row via a distinct new-key attempt.
    agent_stack.script.append(FakeModelTurn(content="unreachable"))
    with pytest.raises(AgentSessionExpiredError):
        await agent_stack.service.submit_turn(org.id, session.id, SubmitTurnRequest(content="q2"))
    expired = await agent_stack.service.get_session(org.id, session.id)
    assert expired.state.value == "EXPIRED"

    replay = await agent_stack.service.submit_turn(org.id, session.id, body)
    assert replay.model_dump() == original.model_dump()


# ---------------------------------------------------------------- TEST 5 / 6


async def test_old_key_different_payload_after_terminal_session_is_conflict_not_invalid_state(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    agent = await _provision(agent_stack, org.id)
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    agent_stack.script.append(FakeModelTurn(content="answer"))
    await agent_stack.service.submit_turn(
        org.id, session.id, SubmitTurnRequest(content="alpha", idempotency_key="k-conflict")
    )
    await agent_stack.service.stop_session(org.id, session.id, StopAgentSessionRequest())

    with pytest.raises(AgentIdempotencyConflictError):
        await agent_stack.service.submit_turn(
            org.id,
            session.id,
            SubmitTurnRequest(content="BETA different", idempotency_key="k-conflict"),
        )


async def test_terminal_session_new_key_rejects_new_execution(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    agent = await _provision(agent_stack, org.id)
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    await agent_stack.service.stop_session(org.id, session.id, StopAgentSessionRequest())

    agent_stack.script.append(FakeModelTurn(content="must not run"))
    with pytest.raises(AgentInvalidStateError):
        await agent_stack.service.submit_turn(
            org.id, session.id, SubmitTurnRequest(content="q", idempotency_key="brand-new-key")
        )
    assert len(agent_stack.provider.calls) == 0


# ---------------------------------------------------------------- TEST 8


async def test_replay_does_not_mutate_accounting_or_reemit_events(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_tool(agent_stack, org.id, mock_http_server)
    mock_http_server.set_handler(lambda m, p, h, b: (200, {"id": "x"}))
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.get",))
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    body = SubmitTurnRequest(content="q", idempotency_key="k-snapshot")
    agent_stack.script.append(
        FakeModelTurn(tool_calls=(("crm.get", {"path_params": {"id": "z"}}),))
    )
    agent_stack.script.append(FakeModelTurn(content="done"))
    await agent_stack.service.submit_turn(org.id, session.id, body)

    before = await _snapshot(agent_stack, org.id, session.id)
    for _ in range(3):
        replay = await agent_stack.service.submit_turn(org.id, session.id, body)
        assert replay.content == "done"
    after = await _snapshot(agent_stack, org.id, session.id)
    assert before == after
    assert len(agent_stack.provider.calls) == 2  # unchanged by any of the 3 replays


# ---------------------------------------------------------------- TEST 9 / 10


async def test_failed_key_replay_after_session_also_terminalised(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    _compress(agent_stack.service, turn_deadline_seconds=0.4)
    agent = await _provision(agent_stack, org.id)
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    body = SubmitTurnRequest(content="q", idempotency_key="k-failed")
    agent_stack.script.append(FakeModelTurn(content="slow", hang_seconds=3.0))
    with pytest.raises(Exception):  # noqa: B017 - AgentTurnTimeoutError
        await agent_stack.service.submit_turn(org.id, session.id, body)
    _compress(agent_stack.service, turn_deadline_seconds=120.0)
    calls_before = len(agent_stack.provider.calls)

    # the SESSION itself also becomes terminal afterwards.
    await agent_stack.service.stop_session(org.id, session.id, StopAgentSessionRequest())

    agent_stack.script.append(FakeModelTurn(content="must not run"))
    with pytest.raises(AgentIdempotentReplayError) as excinfo:
        await agent_stack.service.submit_turn(org.id, session.id, body)
    assert excinfo.value.extensions["turn_state"] == "FAILED"
    assert len(agent_stack.provider.calls) == calls_before  # no rerun


async def test_cancelled_key_replay_after_session_also_terminalised(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    agent = await _provision(agent_stack, org.id)
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    body = SubmitTurnRequest(content="q", idempotency_key="k-cancelled-turn")
    agent_stack.script.append(FakeModelTurn(content="slow", hang_seconds=3.0))
    task = asyncio.create_task(agent_stack.service.submit_turn(org.id, session.id, body))
    await asyncio.sleep(0.2)
    agent_stack.service._turn_tasks[session.id].cancel()
    with pytest.raises(BaseException):  # noqa: B017 - CancelledError
        await task
    calls_before = len(agent_stack.provider.calls)

    # the SESSION itself also becomes terminal afterwards.
    await agent_stack.service.cancel_session(org.id, session.id)

    agent_stack.script.append(FakeModelTurn(content="must not run"))
    with pytest.raises(AgentIdempotentReplayError) as excinfo:
        await agent_stack.service.submit_turn(org.id, session.id, body)
    assert excinfo.value.extensions["turn_state"] == "CANCELLED"
    assert len(agent_stack.provider.calls) == calls_before  # no rerun


# ---------------------------------------------------------------- table-driven sequence


async def test_state_sequence_invariants_hold_across_the_full_lifecycle(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    """start -> submit K -> complete -> replay K -> stop -> replay K -> new K2 (rejected,
    terminal session) -> replay K (still valid, unaffected by K2's rejection). Asserts the
    governing invariants after EVERY transition, not just the terminal state."""
    org = await make_organization()
    principal = await make_tool_principal(org)
    agent = await _provision(agent_stack, org.id)
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )

    k = SubmitTurnRequest(content="q", idempotency_key="k-sequence", correlation_id="c-seq")
    agent_stack.script.append(FakeModelTurn(content="the answer"))
    original = await agent_stack.service.submit_turn(org.id, session.id, k)
    assert len(agent_stack.provider.calls) == 1  # INV-IDEM-002/003

    replay_1 = await agent_stack.service.submit_turn(org.id, session.id, k)
    assert replay_1.model_dump() == original.model_dump()  # INV-IDEM-005
    assert len(agent_stack.provider.calls) == 1  # INV-IDEM-010: no new model call

    stopped = await agent_stack.service.stop_session(org.id, session.id, StopAgentSessionRequest())
    assert stopped.state.value == "COMPLETED"

    replay_2 = await agent_stack.service.submit_turn(org.id, session.id, k)
    assert replay_2.model_dump() == original.model_dump()  # INV-IDEM-008: survives terminal
    assert len(agent_stack.provider.calls) == 1

    agent_stack.script.append(FakeModelTurn(content="must not run"))
    with pytest.raises(AgentInvalidStateError):  # INV-IDEM-009: new key on terminal session
        await agent_stack.service.submit_turn(
            org.id, session.id, SubmitTurnRequest(content="q2", idempotency_key="k-sequence-2")
        )
    assert len(agent_stack.provider.calls) == 1  # the rejected K2 never touched the model

    replay_3 = await agent_stack.service.submit_turn(org.id, session.id, k)
    assert replay_3.model_dump() == original.model_dump()  # K's replay is unaffected by K2
    assert len(agent_stack.provider.calls) == 1

    async with agent_stack.database.tenant_transaction(org.id) as tenant:
        turn_rows = (
            await tenant.session.execute(
                text("SELECT count(*) FROM ai_agent_turns WHERE session_id = :s"),
                {"s": str(session.id)},
            )
        ).scalar_one()
    assert turn_rows == 1  # INV-IDEM-001/002: exactly one logical turn, ever
