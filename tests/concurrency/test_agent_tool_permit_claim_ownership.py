"""NXS-P13 audit corrective #10 — tool permit claim ownership / no-redispatch
certification.

Corrective #9 made the CREATED-vs-ALREADY_EXISTS branch inside both
``_authorize_model_dispatch`` and ``_authorize_tool_dispatch`` an explicit, named
decision (``_PermitOutcome``) instead of an implicit fallthrough after a suppressed
``IntegrityError``. For MODEL dispatch, ALREADY_EXISTS was correctly wired to REJECT —
the caller must not independently perform a second real, billed provider call. For TOOL
dispatch, ALREADY_EXISTS was left to return normally: the reasoning was that
``AgentToolBridge.execute`` is itself keyed by the P08 Tool Engine's own idempotency
key, so a second attempt reaching it is a no-op re-read, never a second live side
effect.

That reasoning is correct about the EFFECT (P08 does deduplicate), but it does not
satisfy permit-OWNERSHIP semantics: ``AgentRuntime`` still does
``await authorize_tool(...); await self._tools.execute(...)`` unconditionally — a
second identical semantic attempt, after this corrective's fix, MUST NOT be allowed to
even ENTER ``AgentToolBridge.execute`` / the Tool Engine, regardless of what P08 would
have done once inside. P08 idempotency remains DEFENSE-IN-DEPTH, not the mechanism that
fixes duplicate permit ownership.

INV-TOOL-PERMIT-001: only the attempt that durably creates the tool-dispatch permit may
proceed as a NEW P08 dispatch.
INV-TOOL-PERMIT-002: ALREADY_EXISTS is not dispatch authority.
INV-TOOL-PERMIT-003: P08 idempotency is defense-in-depth, not the mechanism that fixes
duplicate permit ownership.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.agents.entities import StartAgentSessionRequest, SubmitTurnRequest
from nexus_ai.agents.errors import AgentCancelledError, AgentInvalidStateError
from nexus_ai.agents.idempotency import arguments_hash
from nexus_ai.agents.models.base import ModelToolCall
from nexus_ai.agents.models.fake import FakeModelTurn
from nexus_ai.agents.service import ExecutionRevoked
from nexus_ai.agents.toolbridge import AgentToolBridge
from nexus_ai.domain.auth.entities import Principal
from tests.concurrency.test_agent_lifecycle_race import _agent_events, _second_service
from tests.concurrency.test_agent_tool_dispatch_fencing import _execs, _wait_until
from tests.integration.test_agent_service import _provision, _register_tool

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _permit_count_for_semantic(
    stack: Any, org_id: Any, turn_id: Any, tool_key: str, arg_hash: str
) -> int:
    async with stack.database.tenant_transaction(org_id) as tenant:
        return int(
            (
                await tenant.session.execute(
                    text(
                        "SELECT count(*) FROM ai_agent_tool_dispatch_permits "
                        "WHERE turn_id = :t AND tool_key = :k AND arguments_hash = :h"
                    ),
                    {"t": str(turn_id), "k": tool_key, "h": arg_hash},
                )
            ).scalar_one()
        )


def _wrap_bridge_execute_counter(bridge: AgentToolBridge) -> list[int]:
    """Monkeypatch ONE bridge instance's ``execute`` to count entries while still
    calling the real implementation — the actual dispatch boundary this corrective's
    mandatory test must observe, independent of whatever P08 idempotency does once
    inside. Returns a single-element mutable counter list; caller restores the
    original in a ``finally``."""
    count = [0]
    original = bridge.execute

    async def _counted(**kwargs: Any) -> Any:
        count[0] += 1
        return await original(**kwargs)

    bridge.execute = _counted  # type: ignore[method-assign]
    return count


async def _wait_until_execs(
    stack: Any, org_id: Any, expected: int, *, timeout: float = 5.0
) -> None:
    """The mock HTTP handler recording a hit (``seen``) only proves the request
    reached the server — the Tool Engine's post-response bookkeeping (writing the
    ``ToolExecutionRecord``) is NOT guaranteed to have committed yet at that instant.
    Poll the real durable count instead of asserting immediately after the HTTP-level
    signal, mirroring this session's own established fix for the identical class of
    mistake in corrective #9's CI-flake investigation."""
    deadline = asyncio.get_running_loop().time() + timeout
    while await _execs(stack, org_id) != expected:
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"timed out waiting for {expected} tool_execution_records")
        await asyncio.sleep(0.01)


async def _model_turn_hang(worker: Any, org_id: Any, session_id: Any) -> tuple[Any, Any]:
    """Start a turn whose SECOND model iteration hangs, returning (task, claimed turn)
    once claimed — used to keep a turn genuinely RUNNING (non-terminal) long enough to
    attempt a duplicate authorization mid-turn, mirroring corrective #9's own
    ``_claim_running_turn`` helper."""
    task = asyncio.create_task(
        worker.submit_turn(org_id, session_id, SubmitTurnRequest(content="go"))
    )
    deadline = asyncio.get_running_loop().time() + 5.0
    while True:
        turns = await worker.list_turns(org_id, session_id, limit=1)
        if turns and turns[0].execution_owner_id is not None:
            return task, turns[0]
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("timed out waiting for the turn to be claimed")
        await asyncio.sleep(0.01)


async def test_fail_first_duplicate_tool_permit_wrongly_returns_success(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    """Corrective #10 §3 — the mandatory fail-first proof, directly against audited
    HEAD f874247404ee319e29ec6b42578574ef090fd4af.

    A genuine RUNNING turn (correct organization, session, turn, execution_owner_id)
    and ONE semantic tool identity (tool_key + arguments_hash). Calling
    ``_authorize_tool_dispatch`` a SECOND time with the identical semantic identity
    MUST be rejected — before this corrective's fix, it wrongly returns normally
    (``_PermitOutcome.ALREADY_EXISTS`` was computed and then discarded via
    ``del outcome``), exactly mirroring the directive's described defect."""
    org = await make_organization()
    principal = await make_tool_principal(org)
    agent = await _provision(agent_stack, org.id)
    worker = agent_stack.service

    session = await worker.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    agent_stack.script.append(FakeModelTurn(content="hang", hang_seconds=5.0))
    task, turn = await _model_turn_hang(agent_stack.service, org.id, session.id)

    try:
        arg_hash = arguments_hash({"path_params": {"id": "x1"}})
        await worker._authorize_tool_dispatch(
            org.id, session.id, turn.id, turn.execution_owner_id, "crm.get", arg_hash, 1
        )
        assert (
            await _permit_count_for_semantic(agent_stack, org.id, turn.id, "crm.get", arg_hash) == 1
        )

        # THE FAIL-FIRST ASSERTION: a second identical semantic attempt must be
        # rejected, not silently treated as success.
        with pytest.raises(AgentInvalidStateError):
            await worker._authorize_tool_dispatch(
                org.id, session.id, turn.id, turn.execution_owner_id, "crm.get", arg_hash, 2
            )
        # exactly one permit row for this semantic slot — the rejected duplicate
        # created nothing.
        assert (
            await _permit_count_for_semantic(agent_stack, org.id, turn.id, "crm.get", arg_hash) == 1
        )
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def test_duplicate_tool_permit_never_reaches_toolbridge_execute(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    """Corrective #10 §6 — the MANDATORY end-to-end dispatch-boundary test. Does NOT
    repeat the insufficient prior proof (calling ``_authorize_tool_dispatch`` twice and
    checking only the permit row count) — this test instruments the REAL dispatch
    boundary (``AgentToolBridge.execute``) and proves a second identical semantic
    attempt is rejected BEFORE it ever enters that boundary, not merely deduplicated
    once inside by P08's own idempotency key.

    Also doubles as corrective #10 §8 Case C: the permit already exists, the session is
    still ACTIVE (non-terminal), and a stale/duplicate attempt retries — no fresh
    dispatch may result."""
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_tool(agent_stack, org.id, mock_http_server)
    seen: list[str] = []

    def handler(method: str, path: str, headers: Any, body: Any) -> tuple[int, dict[str, Any]]:
        seen.append(path)
        return 200, {"id": path.rsplit("/", 1)[-1]}

    mock_http_server.set_handler(handler)

    worker = agent_stack.service
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.get",))
    session = await worker.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    # iteration 0: real tool dispatch (fast) — genuinely exercises AgentToolBridge,
    # ToolEngine, and the mock HTTP server once. iteration 1: hangs, keeping the turn
    # RUNNING (non-terminal) long enough to attempt a duplicate mid-turn.
    agent_stack.script.append(
        FakeModelTurn(tool_calls=(("crm.get", {"path_params": {"id": "x1"}}),))
    )
    agent_stack.script.append(FakeModelTurn(content="hang", hang_seconds=5.0))

    bridge_calls = _wrap_bridge_execute_counter(worker._bridge)
    try:
        task_a = asyncio.create_task(
            worker.submit_turn(org.id, session.id, SubmitTurnRequest(content="go"))
        )
        await _wait_until(lambda: len(seen) >= 1)
        await _wait_until_execs(agent_stack, org.id, 1)
        arg_hash = arguments_hash({"path_params": {"id": "x1"}})

        turns = await worker.list_turns(org.id, session.id, limit=1)
        turn = turns[0]
        assert turn.execution_owner_id is not None

        # baseline, BEFORE the duplicate attempt: exactly one real dispatch happened.
        assert bridge_calls[0] == 1
        assert seen == ["/c/x1"]
        assert await _execs(agent_stack, org.id) == 1
        assert (
            await _permit_count_for_semantic(agent_stack, org.id, turn.id, "crm.get", arg_hash) == 1
        )

        # THE DISPATCH-BOUNDARY PROOF: mirror AgentRuntime's own production pattern —
        # authorize, THEN (only if authorize did not raise) dispatch. If the fix is
        # correct, the second attempt's authorize call raises and `bridge.execute` is
        # NEVER reached a second time for this semantic slot.
        row_before = await worker.get_session(org.id, session.id)
        assert row_before.state.value == "ACTIVE"  # Case C: session still live

        principal_2 = Principal(
            user_id=session.initiator_user_id,
            session_id=session.initiator_session_id,
            organization_id=org.id,
            token_id=principal.token_id,
            issued_at=session.created_at,
            expires_at=principal.expires_at,
        )
        with pytest.raises(AgentInvalidStateError):
            await worker._authorize_tool_dispatch(
                org.id, session.id, turn.id, turn.execution_owner_id, "crm.get", arg_hash, 99
            )
            # UNREACHABLE if the fix is correct — proves the guard is the raise
            # itself, not merely a test-side convention:
            await worker._bridge.execute(  # pragma: no cover - defect path only
                principal=principal_2,
                allow_list=frozenset({"crm.get"}),
                call=ModelToolCall(
                    id="dup", name="crm.get", arguments={"path_params": {"id": "x1"}}
                ),
                correlation_id=None,
                idempotency_seed=f"{session.id}:{turn.sequence}",
            )

        # THE ASSERTIONS THE DIRECTIVE REQUIRES (§6/§10/§11/§12/§13):
        assert bridge_calls[0] == 1  # AgentToolBridge/ToolEngine entry count == 1
        assert await _execs(agent_stack, org.id) == 1  # ToolExecutionRecords == 1
        assert seen == ["/c/x1"]  # external HTTP hit count == 1 (== business effect)
        assert (
            await _permit_count_for_semantic(agent_stack, org.id, turn.id, "crm.get", arg_hash) == 1
        )  # tool permit rows == 1

        events = await _agent_events(agent_stack, org.id, "agent.tool.%")
        assert events.count("agent.tool.requested") == 1
        assert events.count("agent.tool.completed") == 1
        assert events.count("agent.tool.failed") == 0

        task_a.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task_a
    finally:
        worker._bridge.execute = AgentToolBridge.execute.__get__(worker._bridge)


async def test_two_worker_stale_execution_attempt_never_reaches_p08(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    """Corrective #10 §7 — the two-worker duplicate test. TWO independent AgentService
    instances share one PostgreSQL database. Worker A owns the real turn and creates
    the tool permit. Worker B — simulating a stale execution attempt (a retried
    request, a resumed stale worker, or a duplicate internal replay) — presents the
    IDENTICAL organization_id/session_id/turn_id/execution_owner_id/tool_key/
    arguments_hash. No process-local lock is required for correctness: the durable
    permit's own unique index is what rejects Worker B, and (after this corrective)
    the authorization layer itself — not merely P08 — refuses to let Worker B's
    attempt reach the Tool Engine at all."""
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_tool(agent_stack, org.id, mock_http_server)
    seen: list[str] = []

    def handler(method: str, path: str, headers: Any, body: Any) -> tuple[int, dict[str, Any]]:
        seen.append(path)
        return 200, {"id": path.rsplit("/", 1)[-1]}

    mock_http_server.set_handler(handler)

    worker_a = agent_stack.service
    worker_b, _pb = _second_service(agent_stack)
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.get",))
    session = await worker_a.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    agent_stack.script.append(
        FakeModelTurn(tool_calls=(("crm.get", {"path_params": {"id": "w1"}}),))
    )
    agent_stack.script.append(FakeModelTurn(content="hang", hang_seconds=5.0))

    bridge_a_calls = _wrap_bridge_execute_counter(worker_a._bridge)
    bridge_b_calls = _wrap_bridge_execute_counter(worker_b._bridge)
    try:
        task_a = asyncio.create_task(
            worker_a.submit_turn(org.id, session.id, SubmitTurnRequest(content="go"))
        )
        await _wait_until(lambda: len(seen) >= 1)
        await _wait_until_execs(agent_stack, org.id, 1)
        arg_hash = arguments_hash({"path_params": {"id": "w1"}})
        turns = await worker_a.list_turns(org.id, session.id, limit=1)
        turn = turns[0]

        # Worker B: the exact same semantic identity Worker A already owns.
        with pytest.raises(AgentInvalidStateError):
            await worker_b._authorize_tool_dispatch(
                org.id, session.id, turn.id, turn.execution_owner_id, "crm.get", arg_hash, 77
            )

        assert bridge_a_calls[0] == 1
        assert bridge_b_calls[0] == 0  # Worker B's attempt never entered its own bridge
        assert await _execs(agent_stack, org.id) == 1
        assert seen == ["/c/w1"]
        assert (
            await _permit_count_for_semantic(agent_stack, org.id, turn.id, "crm.get", arg_hash) == 1
        )

        task_a.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task_a
    finally:
        worker_a._bridge.execute = AgentToolBridge.execute.__get__(worker_a._bridge)
        worker_b._bridge.execute = AgentToolBridge.execute.__get__(  # type: ignore[method-assign]
            worker_b._bridge
        )
        await worker_b.shutdown()


async def test_cancel_first_still_blocks_tool_permit_and_dispatch(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    """Corrective #10 §8 Case B regression (corrective #7's own linearization,
    unmodified by this corrective): cancellation committing BEFORE the tool
    authorization attempt still yields zero permit and zero dispatch — this
    corrective changes ONLY the ALREADY_EXISTS branch, never the ExecutionRevoked /
    session-terminal branch."""
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_tool(agent_stack, org.id, mock_http_server)
    worker = agent_stack.service
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.get",))
    session = await worker.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    agent_stack.script.append(FakeModelTurn(content="hang", hang_seconds=5.0))
    task, turn = await _model_turn_hang(worker, org.id, session.id)

    try:
        cancelled = await worker.cancel_session(org.id, session.id)
        assert cancelled.state.value == "CANCELLED"

        # cancel_session's direct task.cancel() on the tracked _run_turn task makes
        # submit_turn's own `except asyncio.CancelledError: ...; raise` bare-reraise
        # the ORIGINAL CancelledError (not AgentCancelledError) — matching the exact
        # precedent in tests/concurrency/test_agent_concurrency.py.
        with pytest.raises((AgentCancelledError, asyncio.CancelledError)):
            await task

        arg_hash = arguments_hash({"path_params": {"id": "z1"}})
        with pytest.raises(ExecutionRevoked):
            await worker._authorize_tool_dispatch(
                org.id, session.id, turn.id, turn.execution_owner_id, "crm.get", arg_hash, 1
            )
        assert (
            await _permit_count_for_semantic(agent_stack, org.id, turn.id, "crm.get", arg_hash) == 0
        )
    finally:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
