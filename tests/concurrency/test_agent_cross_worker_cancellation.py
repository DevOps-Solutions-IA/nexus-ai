"""NXS-P13 audit corrective #6 — distributed execution control / cancellation
certification.

Fail-first against c6c1c3de664000624461dcacf30e2aae14328a8c: a cross-worker
``cancel_session`` / ``stop_session`` / lifetime expiry committed to PostgreSQL was never
observed by a DIFFERENT worker's already-running turn task until it reached
``_finish_turn`` — so a stale worker's tool loop could keep calling the model AND
dispatching NEW tool calls to the NXS-P08 Tool Engine after the session had already been
authoritatively terminalised elsewhere. Discarding the final response (corrective #3) is
not sufficient: an external side effect may already have happened after cancellation.

INV-CANCEL-001..009, INV-LEASE-001, INV-CRASH-001 (ADR-0094 "Distributed cancellation").
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.agents.entities import (
    AgentChannel,
    StartAgentSessionRequest,
    StopAgentSessionRequest,
    SubmitTurnRequest,
)
from nexus_ai.agents.errors import AgentCancelledError, AgentSessionExpiredError
from nexus_ai.agents.models.fake import FakeModelTurn
from nexus_ai.agents.state_machine import AgentSessionState
from tests.concurrency.test_agent_lifecycle_race import (
    _agent_events,
    _compress,
    _second_service,
    _session_row,
)
from tests.integration.test_agent_service import _provision, _register_tool

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _execs(stack: Any, org_id: Any) -> int:
    async with stack.database.tenant_transaction(org_id) as tenant:
        return (
            await tenant.session.execute(text("SELECT count(*) FROM tool_execution_records"))
        ).scalar_one()


async def _lease_row(stack: Any, org_id: Any, session_id: Any) -> Any:
    async with stack.database.tenant_transaction(org_id) as tenant:
        return (
            await tenant.session.execute(
                text(
                    "SELECT state, execution_owner_id, lease_expires_at "
                    "FROM ai_agent_turns WHERE session_id = :s ORDER BY sequence DESC LIMIT 1"
                ),
                {"s": str(session_id)},
            )
        ).one()


async def _active_turn_id(stack: Any, org_id: Any, session_id: Any) -> Any:
    async with stack.database.tenant_transaction(org_id) as tenant:
        return (
            await tenant.session.execute(
                text(
                    "SELECT id FROM ai_agent_turns WHERE session_id = :s "
                    "AND state NOT IN ('COMPLETED','FAILED','CANCELLED')"
                ),
                {"s": str(session_id)},
            )
        ).scalar_one_or_none()


def _handler_counter() -> tuple[list[str], Any]:
    seen: list[str] = []

    def handler(method: str, path: str, headers: Any, body: Any) -> tuple[int, dict[str, Any]]:
        seen.append(path)
        return 200, {"id": path.rsplit("/", 1)[-1]}

    return seen, handler


async def test_cross_worker_cancel_blocks_tool_after_continuation(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    """The mandatory two-worker test (corrective #6 §7): tool #1 executes, the
    continuation model call is in flight, Worker B cancels, and Worker A's released
    continuation requests tool #2 — tool #2 must NEVER execute."""
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_tool(agent_stack, org.id, mock_http_server)
    seen, handler = _handler_counter()
    mock_http_server.set_handler(handler)

    worker_a = agent_stack.service
    worker_b, _pb = _second_service(agent_stack)
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.get",))
    session = await worker_a.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )

    agent_stack.script.append(
        FakeModelTurn(tool_calls=(("crm.get", {"path_params": {"id": "t1"}}),))
    )
    agent_stack.script.append(
        FakeModelTurn(tool_calls=(("crm.get", {"path_params": {"id": "t2"}}),), hang_seconds=1.0)
    )

    task_a = asyncio.create_task(
        worker_a.submit_turn(org.id, session.id, SubmitTurnRequest(content="go"))
    )
    await asyncio.sleep(0.4)  # tool #1 has executed; iteration 1's model call is hanging

    cancelled = await worker_b.cancel_session(org.id, session.id)
    assert cancelled.state is AgentSessionState.CANCELLED

    with pytest.raises(AgentCancelledError):
        await task_a

    assert seen == ["/c/t1"]  # tool #2 NEVER reached the mock server
    assert await _execs(agent_stack, org.id) == 1

    row = await _session_row(agent_stack, org.id, session.id)
    assert row.state == "CANCELLED"
    assert row.response_text_present is False  # no stale response_text

    turns = await worker_a.list_turns(org.id, session.id, limit=5)
    assert turns[0].state.value == "CANCELLED"  # truthful terminal state
    assert turns[0].error_code == "NXS_AGENT_CANCELLED"

    events = await _agent_events(agent_stack, org.id, "agent.%")
    assert events.count("agent.response.ready") == 0
    assert events.count("agent.session.cancelled") == 1  # not duplicated
    assert events.count("agent.turn.failed") == 1  # not duplicated

    await worker_b.shutdown()


async def test_cross_worker_stop_blocks_tool_after_continuation(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    """Same guarantee for a normal stop() (not a cancellation) — truthful invalid-state,
    never a fabricated cancellation or expiration."""
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_tool(agent_stack, org.id, mock_http_server)
    seen, handler = _handler_counter()
    mock_http_server.set_handler(handler)

    worker_a = agent_stack.service
    worker_b, _pb = _second_service(agent_stack)
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.get",))
    session = await worker_a.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    agent_stack.script.append(
        FakeModelTurn(tool_calls=(("crm.get", {"path_params": {"id": "s1"}}),))
    )
    agent_stack.script.append(
        FakeModelTurn(tool_calls=(("crm.get", {"path_params": {"id": "s2"}}),), hang_seconds=1.0)
    )
    task_a = asyncio.create_task(
        worker_a.submit_turn(org.id, session.id, SubmitTurnRequest(content="go"))
    )
    await asyncio.sleep(0.4)

    stopped = await worker_b.stop_session(org.id, session.id, StopAgentSessionRequest())
    assert stopped.state is AgentSessionState.COMPLETED

    with pytest.raises(Exception):  # noqa: B017 - AgentInvalidStateError
        await task_a

    assert seen == ["/c/s1"]
    row = await _session_row(agent_stack, org.id, session.id)
    assert row.state == "COMPLETED"
    turns = await worker_a.list_turns(org.id, session.id, limit=5)
    assert turns[0].error_code == "NXS_AGENT_INVALID_STATE"  # never a fabricated cancel

    await worker_b.shutdown()


async def test_cross_worker_expiry_blocks_tool_after_continuation(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    """Same guarantee when a SECOND worker's admission check terminalises the session
    EXPIRED while Worker A's turn is still executing."""
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_tool(agent_stack, org.id, mock_http_server)
    seen, handler = _handler_counter()
    mock_http_server.set_handler(handler)

    worker_a = agent_stack.service  # default lifetime: 3600s — A alone never expires
    worker_b, provider_b = _second_service(agent_stack)
    _compress(worker_b, max_session_seconds=1.0)
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.get",))
    session = await worker_a.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    agent_stack.script.append(
        FakeModelTurn(tool_calls=(("crm.get", {"path_params": {"id": "e1"}}),))
    )
    agent_stack.script.append(
        FakeModelTurn(tool_calls=(("crm.get", {"path_params": {"id": "e2"}}),), hang_seconds=2.0)
    )
    task_a = asyncio.create_task(
        worker_a.submit_turn(org.id, session.id, SubmitTurnRequest(content="go"))
    )
    await asyncio.sleep(0.3)  # tool #1 executed, iteration 1 hanging

    await asyncio.sleep(1.0)  # worker_b's 1s lifetime has now passed
    provider_b.script.append(FakeModelTurn(content="never runs"))
    with pytest.raises(AgentSessionExpiredError):
        await worker_b.submit_turn(org.id, session.id, SubmitTurnRequest(content="b"))

    row = await _session_row(agent_stack, org.id, session.id)
    assert row.state == "EXPIRED"

    with pytest.raises(AgentSessionExpiredError):
        await task_a

    assert seen == ["/c/e1"]  # tool #2 never dispatched
    turns = await worker_a.list_turns(org.id, session.id, limit=5)
    assert turns[0].error_code == "NXS_AGENT_SESSION_EXPIRED"

    await worker_b.shutdown()


async def test_already_dispatched_tool_completes_but_nothing_further_dispatches(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    """Corrective #6 §8 boundary proof (part 1): an external call already IN FLIGHT when
    cancellation commits is not retracted — but no operation dispatches after it. The mock
    server itself hangs mid-request so cancellation genuinely races a live dispatch."""
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_tool(agent_stack, org.id, mock_http_server)
    import time as _time

    seen: list[str] = []

    def slow_handler(method: str, path: str, headers: Any, body: Any) -> tuple[int, dict[str, Any]]:
        seen.append(path)
        # runs on the mock server's OWN thread (ThreadingHTTPServer) — this blocks only
        # that thread, leaving the event loop free to run worker_b.cancel_session()
        # concurrently, so the delay genuinely races a live in-flight dispatch.
        _time.sleep(0.6)
        return 200, {"id": path.rsplit("/", 1)[-1]}

    mock_http_server.set_handler(slow_handler)

    worker_a = agent_stack.service
    worker_b, _pb = _second_service(agent_stack)
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.get",))
    session = await worker_a.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    agent_stack.script.append(
        FakeModelTurn(tool_calls=(("crm.get", {"path_params": {"id": "d1"}}),))
    )
    # a SECOND, distinct tool call — this is what actually distinguishes "the new
    # checkpoint blocked it" from "corrective #3's pre-existing final-response absorption
    # would have discarded a plain final answer anyway" (red-team corrective #6 finding:
    # a single-tool-call version of this test cannot tell the two apart).
    agent_stack.script.append(
        FakeModelTurn(tool_calls=(("crm.get", {"path_params": {"id": "d2"}}),))
    )

    task_a = asyncio.create_task(
        worker_a.submit_turn(org.id, session.id, SubmitTurnRequest(content="go"))
    )
    await asyncio.sleep(0.15)  # the checkpoint has already passed; tool #1 is dispatching
    cancelled = await worker_b.cancel_session(org.id, session.id)
    assert cancelled.state is AgentSessionState.CANCELLED

    # the ALREADY-DISPATCHED tool #1 call is allowed to complete (documented boundary) —
    # but tool #2 (a genuinely NEW dispatch, requested only after cancellation already
    # committed) is blocked by the checkpoint immediately preceding it. Without that
    # checkpoint tool #2 WOULD reach the mock server (this is the assertion the red-team
    # finding required: it must actually fail against a build missing the checkpoint).
    with pytest.raises(AgentCancelledError):
        await task_a

    assert seen == ["/c/d1"]  # tool #1 completed despite the race; tool #2 NEVER dispatched
    assert await _execs(agent_stack, org.id) == 1  # exactly the pre-cancel effect, no more
    turns = await worker_a.list_turns(org.id, session.id, limit=5)
    assert turns[0].state.value == "CANCELLED"
    assert turns[0].response_text is None  # no stale "final" content committed

    await worker_b.shutdown()


async def test_voice_channel_barge_in_cross_worker_cancel(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    """P12 voice barge-in obeys the IDENTICAL generic cancellation semantics — no
    ElevenLabs-specific structure in P13, just channel=VOICE through the same
    cancel_session() API."""
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_tool(agent_stack, org.id, mock_http_server)
    seen, handler = _handler_counter()
    mock_http_server.set_handler(handler)

    worker_a = agent_stack.service
    worker_b, _pb = _second_service(agent_stack)
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.get",))
    session = await worker_a.start_session(
        org.id,
        principal,
        StartAgentSessionRequest(agent_id=agent.id, channel=AgentChannel.VOICE),
    )
    agent_stack.script.append(
        FakeModelTurn(tool_calls=(("crm.get", {"path_params": {"id": "v1"}}),))
    )
    agent_stack.script.append(
        FakeModelTurn(tool_calls=(("crm.get", {"path_params": {"id": "v2"}}),), hang_seconds=1.0)
    )
    task_a = asyncio.create_task(
        worker_a.submit_turn(org.id, session.id, SubmitTurnRequest(content="speak"))
    )
    await asyncio.sleep(0.4)

    # barge-in on a DIFFERENT backend worker — the same generic cancel_session() a voice
    # transport handler would call; P13 has no separate "voice cancel" path.
    cancelled = await worker_b.cancel_session(org.id, session.id)
    assert cancelled.state is AgentSessionState.CANCELLED

    with pytest.raises(AgentCancelledError):
        await task_a

    assert seen == ["/c/v1"]  # no further tool dispatch after barge-in
    row = await _session_row(agent_stack, org.id, session.id)
    assert row.response_text_present is False  # no stale text response
    turns = await worker_a.list_turns(org.id, session.id, limit=5)
    assert turns[0].error_code == "NXS_AGENT_CANCELLED"  # canonical taxonomy code

    await worker_b.shutdown()


async def test_running_turn_claims_a_durable_execution_owner_and_lease(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    """The durable lease primitive (INV-LEASE-001 foundation) is set once at claim time,
    before the model is ever called."""
    org = await make_organization()
    principal = await make_tool_principal(org)
    agent = await _provision(agent_stack, org.id)
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    agent_stack.script.append(FakeModelTurn(content="answer", hang_seconds=0.3))
    task = asyncio.create_task(
        agent_stack.service.submit_turn(org.id, session.id, SubmitTurnRequest(content="q"))
    )
    await asyncio.sleep(0.05)

    row = await _lease_row(agent_stack, org.id, session.id)
    assert row.state == "RUNNING"
    assert row.execution_owner_id is not None
    assert row.lease_expires_at is not None
    assert row.lease_expires_at > row.lease_expires_at.__class__.now(row.lease_expires_at.tzinfo)

    await task


async def test_orphaned_running_turn_is_unambiguous_but_p13_does_not_reap_it(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    """INV-CRASH-001 / P13-vs-P25 boundary: simulate a worker that crashed mid-turn (the
    row stays RUNNING forever — nobody ever calls ``_fail_turn``). Its lease is a durable,
    unambiguous "safe to reap" signal for a FUTURE recovery mechanism — but P13 itself
    performs NO autonomous reaping: the session stays BUSY indefinitely, exactly the
    documented, deferred (NXS-P25) limitation."""
    org = await make_organization()
    principal = await make_tool_principal(org)
    agent = await _provision(agent_stack, org.id)
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    # a turn that "crashes": inserted RUNNING directly, exactly as _open_turn would, but
    # NEVER driven through _run_turn / _fail_turn / _finish_turn — no worker will ever
    # commit a terminal state for it, simulating a process that died mid-execution.
    import datetime as dt
    import uuid as uuidlib

    from nexus_ai.domain.agents.repository import AgentSessionRepository, AgentTurnRepository

    async with agent_stack.database.tenant_transaction(org.id) as tenant:
        await AgentTurnRepository(tenant).insert(
            {
                "session_id": session.id,
                "sequence": 1,
                "state": "RUNNING",
                "channel": "API",
                "input_text": "orphaned",
                "response_text": None,
                "input_char_count": 8,
                "idempotency_key": None,
                "request_fingerprint": None,
                "execution_owner_id": uuidlib.uuid7(),
                "lease_expires_at": dt.datetime.now(dt.UTC) - dt.timedelta(seconds=5),
            }
        )
        await AgentSessionRepository(tenant).apply(session.id, {"turn_count": 1})

    row = await _lease_row(agent_stack, org.id, session.id)
    assert row.state == "RUNNING"
    # UNAMBIGUOUS orphan signal: RUNNING + lease already expired. A future P25 reaper can
    # safely act on this because no legitimately-alive worker's own asyncio.timeout could
    # still be running past its own committed lease bound.
    assert row.lease_expires_at < row.lease_expires_at.__class__.now(row.lease_expires_at.tzinfo)

    # P13 documented limitation: it does NOT reap this. The session stays BUSY forever —
    # a new turn is refused, exactly the deferred, non-overclaimed behaviour.
    from nexus_ai.agents.errors import AgentBusyError

    with pytest.raises(AgentBusyError):
        await agent_stack.service.submit_turn(org.id, session.id, SubmitTurnRequest(content="q"))

    active_id = await _active_turn_id(agent_stack, org.id, session.id)
    assert active_id is not None  # still "active" per current P13 semantics — undisturbed
