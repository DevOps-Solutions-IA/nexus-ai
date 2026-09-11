"""NXS-P13 audit corrective #8 — linearizable model-dispatch authority / final
external-execution authority certification.

Corrective #7 closed the TOCTOU race for NEW P08 tool dispatches with a durable
linearization point. The IDENTICAL race remained open for NEW model-provider
invocations: corrective #6's ``_check_execution_authority`` was a cheap, UNLOCKED read
at the top of every continuation iteration — freshness, not fencing. This corrective
replaces it with ``AgentService._authorize_model_dispatch``: the model-invocation
counterpart to corrective #7's tool-dispatch permit, using the identical mechanism
(same session-row ``FOR UPDATE`` lock, same durable permit table, same
short-transaction-never-held-across-the-network discipline) so BOTH classes of external
work P13 can dispatch now share one provably-linearizable authority boundary.

Also adds a turn-authority / stale-worker fencing check (``_assert_turn_authority``),
shared by both the model and tool linearization points, establishing the CONTRACT a
future NXS-P25 orphan-recovery mechanism depends on — unreachable today (nothing yet
reassigns a turn's ``execution_owner_id`` after claim), but load-bearing infrastructure
that must already be correct.

INV-EXEC-001..014 (ADR-0094 "Model-dispatch linearization" / "Turn authority and future
P25 fencing compatibility").
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.agents.entities import StartAgentSessionRequest, SubmitTurnRequest
from nexus_ai.agents.errors import AgentCancelledError, AgentInvalidStateError
from nexus_ai.agents.models.fake import FakeModelTurn
from nexus_ai.domain.agents.repository import AgentSessionRepository
from tests.concurrency.test_agent_lifecycle_race import _agent_events, _second_service, _session_row
from tests.concurrency.test_agent_tool_dispatch_fencing import _execs, _wait_until
from tests.integration.test_agent_service import _provision, _register_tool

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _model_permit_count(stack: Any, org_id: Any, turn_id: Any) -> int:
    async with stack.database.tenant_transaction(org_id) as tenant:
        return int(
            (
                await tenant.session.execute(
                    text("SELECT count(*) FROM ai_agent_model_dispatch_permits WHERE turn_id = :t"),
                    {"t": str(turn_id)},
                )
            ).scalar_one()
        )


async def _model_permit_iterations(stack: Any, org_id: Any, turn_id: Any) -> list[int]:
    async with stack.database.tenant_transaction(org_id) as tenant:
        rows = (
            await tenant.session.execute(
                text(
                    "SELECT iteration FROM ai_agent_model_dispatch_permits "
                    "WHERE turn_id = :t ORDER BY iteration"
                ),
                {"t": str(turn_id)},
            )
        ).all()
    return [r[0] for r in rows]


def _handler(seen: list[str], *, slow: bool = False) -> Any:
    def handle(method: str, path: str, headers: Any, body: Any) -> tuple[int, dict[str, Any]]:
        seen.append(path)
        if slow:
            import time as _time

            _time.sleep(0.5)
        return 200, {"id": path.rsplit("/", 1)[-1]}

    return handle


async def test_authorize_model_blocks_on_inflight_cancellation_and_correctly_rejects(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    """The mandatory model TOCTOU test (corrective #8 §9), built on the SAME genuine
    PostgreSQL row-lock contention technique as corrective #7's mandatory tool test —
    and simultaneously the "continuation test" cancel-first ordering (§11): iteration 0
    authorizes and runs (fast), tool x1 is authorized and dispatches (slow, real
    in-flight call), Worker B's cancel is held open (row lock acquired, UPDATE not yet
    issued) while iteration 1's ``authorize_model`` attempts the IDENTICAL lock —
    genuinely blocking at the database level, not on Python timing.

    Against the pre-corrective-#8 mechanism (an unlocked ``_check_execution_authority``
    read) this exact scenario was unprotected for the model checkpoint the SAME way
    corrective #7 proved it for tool dispatch. Fail-first verified directly: checking
    out `de5abcf`'s `service.py`/`runtime.py` and running this exact test reproduces a
    second model call (`len(agent_stack.provider.calls) == 2`) after cancellation had
    already committed."""
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_tool(agent_stack, org.id, mock_http_server)
    seen: list[str] = []
    mock_http_server.set_handler(_handler(seen, slow=True))

    worker_a = agent_stack.service
    worker_b, _pb = _second_service(agent_stack)
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.get",))
    session = await worker_a.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    # iteration 0: fast, requests tool x1 (slow, real in-flight dispatch)
    agent_stack.script.append(
        FakeModelTurn(tool_calls=(("crm.get", {"path_params": {"id": "x1"}}),))
    )
    # iteration 1: MUST NEVER be reached — its authorize_model() attempt is what this
    # test proves gets rejected.
    agent_stack.script.append(FakeModelTurn(content="must never run"))

    gate = asyncio.Event()
    original_apply = AgentSessionRepository.apply

    async def _delayed_apply(self: Any, session_id: Any, changes: dict[str, Any]) -> Any:
        if changes.get("state") == "CANCELLED":
            await gate.wait()
        return await original_apply(self, session_id, changes)

    AgentSessionRepository.apply = _delayed_apply  # type: ignore[method-assign]
    try:
        task_a = asyncio.create_task(
            worker_a.submit_turn(org.id, session.id, SubmitTurnRequest(content="go"))
        )
        # wait for tool x1's slow dispatch to genuinely be in flight — proves
        # iteration 0's model call + tool authorization already succeeded.
        await _wait_until(lambda: len(seen) >= 1)

        # Worker B: acquire the FOR UPDATE lock (uncontended — A holds none right now,
        # its tool dispatch is a plain HTTP call, no DB transaction open) and block
        # INSIDE the transaction, right before issuing the UPDATE that would commit
        # CANCELLED — the lock stays held.
        task_b = asyncio.create_task(worker_b.cancel_session(org.id, session.id))
        await asyncio.sleep(0.2)  # B now holds the row lock and is waiting on `gate`

        # tool x1's 0.5s dispatch completes around now; the loop reaches iteration 1
        # and calls authorize_model(1, ...), which attempts the IDENTICAL row's FOR
        # UPDATE — and must genuinely block until B's transaction resolves.
        await asyncio.sleep(0.4)

        gate.set()  # release B: its UPDATE commits CANCELLED, the row lock is freed
        cancelled = await task_b
        assert cancelled.state.value == "CANCELLED"

        with pytest.raises(AgentCancelledError):
            await task_a
    finally:
        AgentSessionRepository.apply = original_apply  # type: ignore[method-assign]

    assert seen == ["/c/x1"]  # tool x1 completed (already dispatched); nothing further
    assert await _execs(agent_stack, org.id) == 1
    assert len(agent_stack.provider.calls) == 1  # iteration 1 NEVER reached the provider
    turns = await worker_a.list_turns(org.id, session.id, limit=5)
    assert turns[0].state.value == "CANCELLED"
    assert turns[0].error_code == "NXS_AGENT_CANCELLED"
    assert await _model_permit_count(agent_stack, org.id, turns[0].id) == 1  # only iteration 0
    assert await _model_permit_iterations(agent_stack, org.id, turns[0].id) == [0]

    events = await _agent_events(agent_stack, org.id, "agent.%")
    assert events.count("agent.response.ready") == 0
    assert events.count("agent.session.cancelled") == 1

    await worker_b.shutdown()


async def test_authorize_model_before_cancel_commit_may_complete(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    """The reverse-order proof for MODEL dispatch (corrective #8 §10): a durable model
    permit COMMITS while the session is still ACTIVE, and only THEN does cancellation
    commit. The already-authorized provider call completes; no FURTHER model or tool
    authorization ever happens afterward."""
    org = await make_organization()
    principal = await make_tool_principal(org)
    worker_a = agent_stack.service
    worker_b, _pb = _second_service(agent_stack)
    agent = await _provision(agent_stack, org.id)
    session = await worker_a.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    # the model call ITSELF hangs — this is what gives Worker B room to commit a
    # cancellation while the ALREADY-AUTHORIZED provider call is still in flight.
    agent_stack.script.append(FakeModelTurn(content="final answer", hang_seconds=0.4))

    task_a = asyncio.create_task(
        worker_a.submit_turn(org.id, session.id, SubmitTurnRequest(content="go"))
    )
    await asyncio.sleep(0.1)  # the permit for iteration 0 has committed; the hang is in progress
    cancelled = await worker_b.cancel_session(org.id, session.id)
    assert cancelled.state.value == "CANCELLED"

    with pytest.raises(AgentCancelledError):
        await task_a

    assert len(agent_stack.provider.calls) == 1  # the already-authorized call completed
    turns = await worker_a.list_turns(org.id, session.id, limit=5)
    assert turns[0].state.value == "CANCELLED"  # the final answer is still suppressed
    assert turns[0].response_text is None
    assert await _model_permit_count(agent_stack, org.id, turns[0].id) == 1

    await worker_b.shutdown()


async def test_continuation_model_authorized_first_but_subsequent_tool_after_cancel_rejected(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    """The SECOND continuation-race ordering (corrective #8 §11): model iteration 1's
    authorization commits (and its provider call completes) BEFORE cancellation commits
    — legitimately allowed to complete — but the TOOL authorization it then requests
    (iteration 1's response wants a tool) must still be rejected once cancellation has
    committed by the time that tool authorization is attempted. Integrates corrective
    #7 (tool fencing) with corrective #8 (model fencing) in one flow."""
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_tool(agent_stack, org.id, mock_http_server)
    seen: list[str] = []
    mock_http_server.set_handler(_handler(seen))

    worker_a = agent_stack.service
    worker_b, _pb = _second_service(agent_stack)
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.get",))
    session = await worker_a.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    # iteration 0 requests tool "seed" (fast, to get a second iteration); iteration 1
    # hangs (its authorize_model() commits BEFORE the hang) and then requests tool
    # "late".
    agent_stack.script.append(
        FakeModelTurn(tool_calls=(("crm.get", {"path_params": {"id": "seed"}}),))
    )
    agent_stack.script.append(
        FakeModelTurn(tool_calls=(("crm.get", {"path_params": {"id": "late"}}),), hang_seconds=0.4)
    )

    task_a = asyncio.create_task(
        worker_a.submit_turn(org.id, session.id, SubmitTurnRequest(content="go"))
    )
    await _wait_until(lambda: len(seen) >= 1)  # tool "seed" has dispatched; iteration 1's
    # model call (hang_seconds=0.4) is now in progress — its authorize_model() ALREADY
    # committed before the hang (authorize-then-call ordering), so it is legitimately
    # authorized regardless of what happens next.
    await asyncio.sleep(0.1)
    cancelled = await worker_b.cancel_session(org.id, session.id)
    assert cancelled.state.value == "CANCELLED"

    with pytest.raises(AgentCancelledError):
        await task_a

    # iteration 1's ALREADY-AUTHORIZED provider call completed (2 calls total: 0 and 1)
    assert len(agent_stack.provider.calls) == 2
    # but the tool it then requested ("late") was NEVER authorized/dispatched — the
    # checkpoint immediately before that NEW tool dispatch observed the by-then-
    # committed cancellation.
    assert seen == ["/c/seed"]
    assert await _execs(agent_stack, org.id) == 1
    turns = await worker_a.list_turns(org.id, session.id, limit=5)
    assert turns[0].state.value == "CANCELLED"
    assert await _model_permit_count(agent_stack, org.id, turns[0].id) == 2  # iterations 0 and 1

    await worker_b.shutdown()


async def test_three_workers_model_and_tool_dispatch_fencing(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    """Corrective #8 §13: THREE independent AgentService instances. Worker A runs the
    turn (model iteration 0 -> tool -> model iteration 1, slow); Worker B cancels;
    Worker C races a duplicate cancel AND a retried submit_turn. Assert correct
    provider-invocation count, correct permit counts (model + tool), no post-cancel
    authorization of either kind, no duplicate events/accounting, stable terminal
    session."""
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_tool(agent_stack, org.id, mock_http_server)
    seen: list[str] = []
    mock_http_server.set_handler(_handler(seen))

    worker_a = agent_stack.service
    worker_b, _pb = _second_service(agent_stack)
    worker_c, provider_c = _second_service(agent_stack)
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.get",))
    session = await worker_a.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    agent_stack.script.append(
        FakeModelTurn(tool_calls=(("crm.get", {"path_params": {"id": "w1"}}),))
    )
    agent_stack.script.append(
        FakeModelTurn(tool_calls=(("crm.get", {"path_params": {"id": "w2"}}),), hang_seconds=0.4)
    )

    task_a = asyncio.create_task(
        worker_a.submit_turn(org.id, session.id, SubmitTurnRequest(content="go"))
    )
    await _wait_until(lambda: len(seen) >= 1)
    await asyncio.sleep(0.1)

    results = await asyncio.gather(
        worker_b.cancel_session(org.id, session.id),
        worker_c.cancel_session(org.id, session.id),
    )
    assert all(r.state.value == "CANCELLED" for r in results)

    provider_c.script.append(FakeModelTurn(content="must never run"))
    with pytest.raises(AgentInvalidStateError):
        await worker_c.submit_turn(org.id, session.id, SubmitTurnRequest(content="retry"))
    assert provider_c.calls == []

    with pytest.raises(AgentCancelledError):
        await task_a

    assert len(agent_stack.provider.calls) == 2  # iterations 0 and 1 — both already authorized
    assert seen == ["/c/w1"]  # tool w2 was never authorized/dispatched
    assert await _execs(agent_stack, org.id) == 1

    turns = await worker_a.list_turns(org.id, session.id, limit=10)
    assert len(turns) == 1  # one logical turn — the retry never created a second
    assert await _model_permit_count(agent_stack, org.id, turns[0].id) == 2

    row = await _session_row(agent_stack, org.id, session.id)
    assert row.state == "CANCELLED"  # stable — the duplicate cancel never re-fired

    events = await _agent_events(agent_stack, org.id, "agent.%")
    assert events.count("agent.session.cancelled") == 1  # not duplicated by the race

    await worker_b.shutdown()
    await worker_c.shutdown()


async def test_model_permit_is_tenant_scoped_and_never_cross_tenant_visible(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    """INV-EXEC-010: a model permit created in one Organization's transaction is
    invisible to a different Organization's tenant-scoped query."""
    org_1 = await make_organization()
    org_2 = await make_organization()
    principal_1 = await make_tool_principal(org_1)
    agent = await _provision(agent_stack, org_1.id)
    session = await agent_stack.service.start_session(
        org_1.id, principal_1, StartAgentSessionRequest(agent_id=agent.id)
    )
    agent_stack.script.append(FakeModelTurn(content="done"))
    response = await agent_stack.service.submit_turn(
        org_1.id, session.id, SubmitTurnRequest(content="go")
    )
    assert response.content == "done"

    assert await _model_permit_count(agent_stack, org_2.id, response.turn_id) == 0
    assert await _model_permit_count(agent_stack, org_1.id, response.turn_id) == 1


async def test_stale_worker_cannot_authorize_model_or_tool_for_a_turn_it_no_longer_owns(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    """Turn-authority fencing (corrective #8 §8, INV-EXEC-007/014): the shared
    ``_assert_turn_authority`` check is otherwise UNREACHABLE through the public API
    today (nothing reassigns ``execution_owner_id`` after claim) — this directly
    exercises it with a deliberately WRONG owner id, proving the contract a future
    NXS-P25 reclaim mechanism will depend on actually rejects a stale claim."""
    import uuid as uuidlib

    org = await make_organization()
    principal = await make_tool_principal(org)
    agent = await _provision(agent_stack, org.id)
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    agent_stack.script.append(FakeModelTurn(content="hang", hang_seconds=0.3))
    task = asyncio.create_task(
        agent_stack.service.submit_turn(org.id, session.id, SubmitTurnRequest(content="q"))
    )
    await asyncio.sleep(0.05)
    turns = await agent_stack.service.list_turns(org.id, session.id, limit=1)
    turn = turns[0]
    assert turn.execution_owner_id is not None

    wrong_owner = uuidlib.uuid7()
    with pytest.raises(AgentInvalidStateError):
        await agent_stack.service._authorize_model_dispatch(
            org.id, session.id, turn.id, wrong_owner, 99, "fake-model"
        )
    with pytest.raises(AgentInvalidStateError):
        await agent_stack.service._authorize_tool_dispatch(
            org.id, session.id, turn.id, wrong_owner, "crm.get", "deadbeef", 99
        )
    # the rejected model-authorization attempt (iteration 99) created no permit — the
    # only permit that exists is the REAL, legitimately-owned turn's own iteration 0,
    # from the concurrently-running task above (not from either rejected call).
    assert 99 not in await _model_permit_iterations(agent_stack, org.id, turn.id)

    await task
