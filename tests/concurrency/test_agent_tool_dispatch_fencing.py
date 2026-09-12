"""NXS-P13 audit corrective #7 — linearizable execution authority / tool-dispatch
fencing.

Corrective #6 closed the cross-worker cancellation gap with a fresh, UNLOCKED read
immediately before every new tool dispatch (``AgentService._check_execution_authority``).
That narrows the TOCTOU window to the (normally microscopic) gap between the read
returning and the dispatch actually beginning — it does NOT close it: nothing prevents a
concurrent cancellation from committing in that gap, and nothing re-verifies once the read
has already returned "ACTIVE". This corrective replaces that checkpoint, for the
tool-dispatch case specifically, with a durable LINEARIZATION POINT
(``AgentService._authorize_tool_dispatch``): a transaction that takes the SAME
``SELECT ... FOR UPDATE`` lock on the session row that ``_terminalize`` (stop / cancel /
expire) takes, and — only if the session is still live under that lock — inserts a durable
``ai_agent_tool_dispatch_permits`` row before committing. Two transactions contending for
the identical row lock are serialized by PostgreSQL itself: whichever commits first is the
objective, durable, provable answer to "did this tool call's authorization happen before or
after the cancellation" — not a race won by chance timing.

INV-FENCE-001..012 (ADR-0094 "Tool-dispatch linearization / fencing").
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.agents.entities import StartAgentSessionRequest, SubmitTurnRequest
from nexus_ai.agents.errors import AgentCancelledError
from nexus_ai.agents.models.fake import FakeModelTurn
from nexus_ai.domain.agents.repository import AgentSessionRepository
from tests.concurrency.test_agent_lifecycle_race import _agent_events, _second_service, _session_row
from tests.integration.test_agent_service import _provision, _register_tool

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _permit_count(stack: Any, org_id: Any, turn_id: Any) -> int:
    async with stack.database.tenant_transaction(org_id) as tenant:
        return int(
            (
                await tenant.session.execute(
                    text("SELECT count(*) FROM ai_agent_tool_dispatch_permits WHERE turn_id = :t"),
                    {"t": str(turn_id)},
                )
            ).scalar_one()
        )


async def _permits(stack: Any, org_id: Any, turn_id: Any) -> list[str]:
    async with stack.database.tenant_transaction(org_id) as tenant:
        rows = (
            await tenant.session.execute(
                text(
                    "SELECT tool_key || ':' || arguments_hash FROM "
                    "ai_agent_tool_dispatch_permits WHERE turn_id = :t ORDER BY sequence"
                ),
                {"t": str(turn_id)},
            )
        ).all()
    return [r[0] for r in rows]


async def _execs(stack: Any, org_id: Any) -> int:
    async with stack.database.tenant_transaction(org_id) as tenant:
        return int(
            (
                await tenant.session.execute(text("SELECT count(*) FROM tool_execution_records"))
            ).scalar_one()
        )


async def _wait_until(predicate: Any, *, timeout: float = 5.0, interval: float = 0.01) -> None:
    """Poll ``predicate()`` until truthy instead of a fixed sleep — the production
    ordering guarantee comes from ``authorize_tool()`` being awaited strictly before
    ``execute()`` in program order (proven by the mandatory row-lock-contention test);
    this only removes CI-load flakiness from the SEQUENCING of the test's own two
    workers, never the guarantee itself."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("timed out waiting for predicate")
        await asyncio.sleep(interval)


def _handler_counter() -> tuple[list[str], Any]:
    seen: list[str] = []

    def handler(method: str, path: str, headers: Any, body: Any) -> tuple[int, dict[str, Any]]:
        seen.append(path)
        return 200, {"id": path.rsplit("/", 1)[-1]}

    return seen, handler


async def test_authorize_blocks_on_inflight_cancellation_and_correctly_rejects(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    """The mandatory exact-linearization test (corrective #7 §8), built on GENUINE
    PostgreSQL row-lock contention rather than Python-level timing: Worker B's cancel
    transaction is held open — its ``SELECT ... FOR UPDATE`` lock already acquired, its
    ``UPDATE`` not yet issued — for a controlled duration, while Worker A's
    ``_authorize_tool_dispatch`` attempts the IDENTICAL row lock. Worker A's transaction
    genuinely BLOCKS at the database level until Worker B's transaction resolves, and only
    then observes the (by then durably committed) CANCELLED state.

    This test, UNCHANGED, is genuinely fail-first against `adda86f`
    (`e494904`..`adda86f`'s pre-corrective-#7 `_check_execution_authority`): an unlocked
    SELECT does not block on another transaction's uncommitted FOR UPDATE lock — it
    returns the last COMMITTED snapshot, which is still ACTIVE while Worker B's cancel is
    held open, so the tool incorrectly dispatches (``seen == ["/c/x1"]``) before Worker
    B's cancel ever commits. Verified directly: checking out `adda86f`'s
    `service.py`/`runtime.py` and running this exact test reproduces that failure
    deterministically (real PostgreSQL row-lock timing, not a probabilistic race).
    """
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
    # The MODEL call itself hangs — this is what gives Worker B room to acquire the row
    # lock and block on `gate` WHILE Worker A holds NO lock at all (Worker A's own
    # `_open_turn` transaction already committed and released its lock before the model
    # was ever called — only the SUBSEQUENT tool-dispatch authorization attempt, made
    # AFTER the hang completes, is what must contend with Worker B).
    agent_stack.script.append(
        FakeModelTurn(tool_calls=(("crm.get", {"path_params": {"id": "x1"}}),), hang_seconds=0.4)
    )

    gate = asyncio.Event()
    original_apply = AgentSessionRepository.apply

    async def _delayed_apply(self: Any, session_id: Any, changes: dict[str, Any]) -> Any:
        if changes.get("state") == "CANCELLED":
            await gate.wait()
        return await original_apply(self, session_id, changes)

    AgentSessionRepository.apply = _delayed_apply  # type: ignore[method-assign]
    try:
        # Worker A: open the turn (fast, uncontended — the session is still ACTIVE and
        # Worker B has not started yet) and enter the model call, which hangs.
        task_a = asyncio.create_task(
            worker_a.submit_turn(org.id, session.id, SubmitTurnRequest(content="go"))
        )
        await asyncio.sleep(0.1)  # _open_turn has committed; the model hang is in progress

        # Worker B: acquire the FOR UPDATE lock (uncontended — A holds none right now)
        # and block INSIDE the transaction, right before issuing the UPDATE that would
        # commit CANCELLED — the lock stays held.
        task_b = asyncio.create_task(worker_b.cancel_session(org.id, session.id))
        await asyncio.sleep(0.2)  # B now holds the row lock and is waiting on `gate`
        # (0.1 + 0.2 = 0.3s elapsed — still inside A's 0.4s model hang)

        # Once A's model call returns (~0.4s), its runtime loop calls authorize_tool(),
        # which attempts the IDENTICAL row's FOR UPDATE — and must genuinely block, not
        # poll, not retry, not time out — until B's transaction resolves.
        await asyncio.sleep(0.3)  # now past the hang; A is blocked waiting for B's lock

        gate.set()  # release B: its UPDATE commits CANCELLED, the row lock is freed
        cancelled = await task_b
        assert cancelled.state.value == "CANCELLED"

        with pytest.raises(AgentCancelledError):
            await task_a
    finally:
        AgentSessionRepository.apply = original_apply  # type: ignore[method-assign]

    assert seen == []  # the tool NEVER reached the mock server
    assert await _execs(agent_stack, org.id) == 0
    turns = await worker_a.list_turns(org.id, session.id, limit=5)
    assert turns[0].state.value == "CANCELLED"
    assert turns[0].error_code == "NXS_AGENT_CANCELLED"
    assert await _permit_count(agent_stack, org.id, turns[0].id) == 0  # no permit was ever created

    events = await _agent_events(agent_stack, org.id, "agent.%")
    assert events.count("agent.response.ready") == 0
    assert events.count("agent.session.cancelled") == 1

    await worker_b.shutdown()


async def test_authorize_before_cancel_commit_may_complete(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    """The reverse-order proof (corrective #7 §9): a durable dispatch permit COMMITS
    while the session is still ACTIVE — a genuine, provable linearization ordering, not a
    lucky race — and THEN cancellation commits. The already-authorized operation
    completes; no FURTHER tool is ever authorized afterward."""
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_tool(agent_stack, org.id, mock_http_server)

    import time as _time

    seen: list[str] = []

    def slow_handler(method: str, path: str, headers: Any, body: Any) -> tuple[int, dict[str, Any]]:
        seen.append(path)
        # a REAL in-flight external call (separate server thread) — proves the permit,
        # not mere luck, is what makes this legitimate: the permit is already durably
        # committed in Postgres well before this handler even starts running.
        _time.sleep(0.5)
        return 200, {"id": path.rsplit("/", 1)[-1]}

    mock_http_server.set_handler(slow_handler)

    worker_a = agent_stack.service
    worker_b, _pb = _second_service(agent_stack)
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.get",))
    session = await worker_a.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    agent_stack.script.append(
        FakeModelTurn(tool_calls=(("crm.get", {"path_params": {"id": "r1"}}),))
    )
    agent_stack.script.append(FakeModelTurn(content="final"))

    task_a = asyncio.create_task(
        worker_a.submit_turn(org.id, session.id, SubmitTurnRequest(content="go"))
    )
    # wait for the dispatch to genuinely be in flight (the handler has been entered) —
    # NOT a fixed sleep: by program order (runtime.py: `await authorize_tool(...)` then
    # `await self._tools.execute(...)`), the permit is ALWAYS committed strictly before
    # the handler can run, so this predicate is a precise, non-flaky proxy for "the
    # permit has committed" regardless of CI scheduling load.
    await _wait_until(lambda: len(seen) >= 1)
    cancelled = await worker_b.cancel_session(org.id, session.id)
    assert cancelled.state.value == "CANCELLED"

    with pytest.raises(AgentCancelledError):
        await task_a

    assert seen == ["/c/r1"]  # the already-authorized dispatch completed
    assert await _execs(agent_stack, org.id) == 1  # exactly one effect — no more
    turns = await worker_a.list_turns(org.id, session.id, limit=5)
    assert turns[0].state.value == "CANCELLED"  # the FINAL answer is still suppressed
    assert turns[0].response_text is None
    assert await _permit_count(agent_stack, org.id, turns[0].id) == 1  # exactly one permit

    await worker_b.shutdown()


async def test_multi_tool_same_response_only_pre_cancel_authorization_survives(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    """Corrective #7 §10: model returns tool A, tool B, tool C in ONE response. A is
    authorized and dispatches (slow, real in-flight call); cancellation commits during
    A's dispatch; B and C — processed sequentially within the SAME loop iteration —
    must never be authorized. No assumption about model-iteration boundaries: this is
    all within a SINGLE FakeModelTurn."""
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_tool(agent_stack, org.id, mock_http_server)

    import time as _time

    seen: list[str] = []

    def handler(method: str, path: str, headers: Any, body: Any) -> tuple[int, dict[str, Any]]:
        seen.append(path)
        if path.endswith("/a1"):
            _time.sleep(0.5)
        return 200, {"id": path.rsplit("/", 1)[-1]}

    mock_http_server.set_handler(handler)

    worker_a = agent_stack.service
    worker_b, _pb = _second_service(agent_stack)
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.get",))
    session = await worker_a.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    agent_stack.script.append(
        FakeModelTurn(
            tool_calls=(
                ("crm.get", {"path_params": {"id": "a1"}}),
                ("crm.get", {"path_params": {"id": "b1"}}),
                ("crm.get", {"path_params": {"id": "c1"}}),
            )
        )
    )

    task_a = asyncio.create_task(
        worker_a.submit_turn(org.id, session.id, SubmitTurnRequest(content="go"))
    )
    # wait for tool A's dispatch to genuinely be in flight — not a fixed sleep; see
    # test_authorize_before_cancel_commit_may_complete's identical rationale.
    await _wait_until(lambda: len(seen) >= 1)
    cancelled = await worker_b.cancel_session(org.id, session.id)
    assert cancelled.state.value == "CANCELLED"

    with pytest.raises(AgentCancelledError):
        await task_a

    assert seen == ["/c/a1"]  # A completed; B and C never reached the mock server
    assert await _execs(agent_stack, org.id) == 1
    turns = await worker_a.list_turns(org.id, session.id, limit=5)
    permits = await _permits(agent_stack, org.id, turns[0].id)
    assert len(permits) == 1  # only A's permit was ever created — B and C were rejected
    assert await _permit_count(agent_stack, org.id, turns[0].id) == 1

    await worker_b.shutdown()


async def test_three_workers_one_authorized_effect_zero_post_cancel_authorizations(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    """Corrective #7 §11: THREE independent AgentService instances share one PostgreSQL
    database. Worker A runs the turn; Worker B cancels; Worker C is an adversarial
    duplicate — it races a SECOND cancel_session call AND attempts a brand-new turn on
    the (by then terminal) session. Assert: one logical turn, at most one authorized
    pre-cancel effect, zero post-cancel authorizations, no duplicate Tool Engine
    effects, a stable terminal session state."""
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_tool(agent_stack, org.id, mock_http_server)

    import time as _time

    seen: list[str] = []

    def handler(method: str, path: str, headers: Any, body: Any) -> tuple[int, dict[str, Any]]:
        seen.append(path)
        _time.sleep(0.5)
        return 200, {"id": path.rsplit("/", 1)[-1]}

    mock_http_server.set_handler(handler)

    worker_a = agent_stack.service
    worker_b, _pb = _second_service(agent_stack)
    worker_c, provider_c = _second_service(agent_stack)
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.get",))
    session = await worker_a.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    agent_stack.script.append(
        FakeModelTurn(tool_calls=(("crm.get", {"path_params": {"id": "z1"}}),))
    )

    task_a = asyncio.create_task(
        worker_a.submit_turn(org.id, session.id, SubmitTurnRequest(content="go"))
    )
    # wait for tool z1's dispatch to genuinely be in flight — not a fixed sleep; see
    # test_authorize_before_cancel_commit_may_complete's identical rationale.
    await _wait_until(lambda: len(seen) >= 1)

    # Worker B and Worker C both attempt to cancel concurrently — a duplicate/adversarial
    # cancellation race. Exactly one canonical terminal transition may occur.
    results = await asyncio.gather(
        worker_b.cancel_session(org.id, session.id),
        worker_c.cancel_session(org.id, session.id),
    )
    assert all(r.state.value == "CANCELLED" for r in results)

    # Worker C also retries with a brand-new turn on the now-terminal session.
    from nexus_ai.agents.errors import AgentInvalidStateError

    provider_c.script.append(FakeModelTurn(content="must never run"))
    with pytest.raises(AgentInvalidStateError):
        await worker_c.submit_turn(org.id, session.id, SubmitTurnRequest(content="retry"))
    assert provider_c.calls == []

    with pytest.raises(AgentCancelledError):
        await task_a

    assert seen == ["/c/z1"]  # exactly the one pre-cancel authorized effect
    assert await _execs(agent_stack, org.id) == 1

    turns = await worker_a.list_turns(org.id, session.id, limit=10)
    assert len(turns) == 1  # one logical turn — the retry never created a second
    assert await _permit_count(agent_stack, org.id, turns[0].id) == 1

    row = await _session_row(agent_stack, org.id, session.id)
    assert row.state == "CANCELLED"  # stable — the duplicate cancel never re-fired

    events = await _agent_events(agent_stack, org.id, "agent.%")
    assert events.count("agent.session.cancelled") == 1  # not duplicated by the race

    await worker_b.shutdown()
    await worker_c.shutdown()


async def test_permit_is_tenant_scoped_and_never_cross_tenant_visible(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    """INV-FENCE-008: a permit created in one Organization's transaction is invisible to
    a different Organization's tenant-scoped query, even though both rows may share a
    physical table."""
    org_1 = await make_organization()
    org_2 = await make_organization()
    principal_1 = await make_tool_principal(org_1)
    await _register_tool(agent_stack, org_1.id, mock_http_server)
    mock_http_server.set_handler(lambda m, p, h, b: (200, {"id": "x"}))

    agent = await _provision(agent_stack, org_1.id, tool_keys=("crm.get",))
    session = await agent_stack.service.start_session(
        org_1.id, principal_1, StartAgentSessionRequest(agent_id=agent.id)
    )
    agent_stack.script.append(
        FakeModelTurn(tool_calls=(("crm.get", {"path_params": {"id": "t1"}}),))
    )
    agent_stack.script.append(FakeModelTurn(content="done"))
    response = await agent_stack.service.submit_turn(
        org_1.id, session.id, SubmitTurnRequest(content="go")
    )
    assert response.tool_calls == 1

    # org_2's tenant-scoped query for the SAME turn id finds nothing — RLS-enforced.
    assert await _permit_count(agent_stack, org_2.id, response.turn_id) == 0
    assert await _permit_count(agent_stack, org_1.id, response.turn_id) == 1
