"""NXS-P13 audit corrective #9 — durable authority identity / permit ownership
certification.

Correctives #7/#8 made tool- and model-dispatch authorization a durable, PostgreSQL
-provable LINEARIZATION POINT: a ``SELECT ... FOR UPDATE`` on the owning
``ai_agent_sessions`` row, taken in the same transaction as the durable permit insert,
so authorize-vs-cancel ordering is an objective fact instead of a check-then-act race.

Neither corrective proved the row being locked is actually the turn's OWN parent
session. ``AgentService._assert_turn_authority`` checks turn-terminality and
``execution_owner_id`` but never checks ``turn.session_id == session_id`` — the
session_id the CALLER passed in and that was locked FOR UPDATE. Within one
Organization, RLS does not help: both the caller-supplied session and the turn's real
parent session belong to the same tenant, so a tenant-scoped read of either succeeds.

Fail-first reproduction (corrective #9 directive Section 2), directly against audited
head 5b80cbc07010e3f7201bce40caf93c9dfdbd37b3: session A is ACTIVE and has NOTHING to do
with turn B; session B is CANCELLED; turn B is a genuine, still-RUNNING, legitimately
owned turn that belongs to session B. Calling ``_authorize_model_dispatch`` /
``_authorize_tool_dispatch`` with ``session_id=A, turn_id=B.id, owner=B's real owner``
locks A (ACTIVE — passes the terminal check for the WRONG session), then checks turn B's
terminality (RUNNING — not yet terminal) and owner (matches) — and, before this
corrective, wrongly authorizes and commits a durable permit despite A never having been
turn B's parent session and B (the session that actually owns this turn) already being
CANCELLED.

INV-AUTH-ID-001 (ADR-0094): a dispatch is authorized only if
``permit.organization_id == turn.organization_id == session.organization_id AND
permit.session_id == turn.session_id == locked_session.id AND
expected_owner == turn.execution_owner_id AND turn is non-terminal AND session is
non-terminal``. No subset of these checks is sufficient.
"""

from __future__ import annotations

import asyncio
import uuid as uuidlib
from typing import Any

import pytest

from nexus_ai.agents.entities import StartAgentSessionRequest, SubmitTurnRequest
from nexus_ai.agents.errors import AgentInvalidStateError
from nexus_ai.agents.models.fake import FakeModelTurn
from nexus_ai.agents.service import ExecutionRevoked
from nexus_ai.agents.state_machine import AgentSessionState, session_rank
from nexus_ai.domain.agents.repository import AgentSessionRepository
from tests.concurrency.test_agent_tool_dispatch_fencing import _execs
from tests.integration.test_agent_service import _provision, _register_tool

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _tool_permit_count(stack: Any, org_id: Any, turn_id: Any) -> int:
    from sqlalchemy import text

    async with stack.database.tenant_transaction(org_id) as tenant:
        return int(
            (
                await tenant.session.execute(
                    text("SELECT count(*) FROM ai_agent_tool_dispatch_permits WHERE turn_id = :t"),
                    {"t": str(turn_id)},
                )
            ).scalar_one()
        )


async def _model_permit_count_for_iteration(
    stack: Any, org_id: Any, turn_id: Any, iteration: int
) -> int:
    """Scoped to ONE specific iteration number, unlike ``_model_permit_count`` (whole
    -turn total): ``_claim_running_turn``'s background task legitimately authorizes its
    OWN iteration 0 at a nondeterministic point relative to a test's own assertions (it
    races the actual production ordering the test does not control), so any test using a
    non-zero iteration for its own manual calls must scope its count to THAT iteration —
    a whole-turn total would flake depending on whether iteration 0's real permit has
    landed yet, exactly the class of test-robustness mistake corrective #8's own CI
    flake taught this session not to repeat."""
    from sqlalchemy import text

    async with stack.database.tenant_transaction(org_id) as tenant:
        return int(
            (
                await tenant.session.execute(
                    text(
                        "SELECT count(*) FROM ai_agent_model_dispatch_permits "
                        "WHERE turn_id = :t AND iteration = :i"
                    ),
                    {"t": str(turn_id), "i": iteration},
                )
            ).scalar_one()
        )


async def _cancel_session_row_directly(stack: Any, org_id: Any, session_id: Any) -> None:
    """Flip ONLY the session row to CANCELLED at the repository boundary — deliberately
    bypassing ``AgentService._terminalize`` (which would also cancel the in-flight turn
    TASK, terminalising the turn itself and masking the identity gap this test targets).
    This reproduces the directive's exact fixture: a genuinely CANCELLED session whose
    (now orphaned, from B's point of view) turn is still legitimately RUNNING."""
    import datetime as dt

    async with stack.database.tenant_transaction(org_id) as tenant:
        await AgentSessionRepository(tenant).apply(
            session_id,
            {
                "state": "CANCELLED",
                "state_rank": session_rank(AgentSessionState.CANCELLED),
                "disposition": None,
                "error_code": "NXS_AGENT_CANCELLED",
                "ended_at": dt.datetime.now(dt.UTC),
            },
        )


async def _claim_running_turn(worker: Any, org_id: Any, session_id: Any) -> tuple[Any, Any]:
    """Start a turn that hangs mid-model-call and return (task, claimed turn row) once
    ``_open_turn`` has durably committed ``execution_owner_id`` — a genuine, legitimately
    -owned, still-RUNNING turn, not a fabricated one."""
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


async def test_wrong_session_cannot_authorize_model_dispatch_for_another_sessions_turn(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    """The mandatory wrong-session fail-first test (corrective #9 Sections 2 / 12).

    Against 5b80cbc: this call MUST NOT raise, and a permit row is wrongly committed —
    proving ``_assert_turn_authority`` authorizes model dispatch for turn B while locking
    a wholly unrelated session A, even though B (turn B's real parent) is CANCELLED.
    After the fix: MUST raise ``AgentInvalidStateError`` and create ZERO permit rows."""
    org = await make_organization()
    principal = await make_tool_principal(org)
    agent = await _provision(agent_stack, org.id)
    worker = agent_stack.service

    session_a = await worker.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    session_b = await worker.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )

    agent_stack.script.append(FakeModelTurn(content="hang", hang_seconds=5.0))
    task_b, turn_b = await _claim_running_turn(worker, org.id, session_b.id)
    assert turn_b.state.value == "RUNNING"

    try:
        await _cancel_session_row_directly(agent_stack, org.id, session_b.id)
        row_a = await worker.get_session(org.id, session_a.id)
        assert row_a.state.value == "ACTIVE"

        with pytest.raises(AgentInvalidStateError):
            await worker._authorize_model_dispatch(
                org.id, session_a.id, turn_b.id, turn_b.execution_owner_id, 1, "fake-model"
            )
        assert await _model_permit_count_for_iteration(agent_stack, org.id, turn_b.id, 1) == 0

        with pytest.raises(AgentInvalidStateError):
            await worker._authorize_tool_dispatch(
                org.id, session_a.id, turn_b.id, turn_b.execution_owner_id, "crm.get", "deadbeef", 1
            )
        assert await _tool_permit_count(agent_stack, org.id, turn_b.id) == 0
    finally:
        task_b.cancel()
        import contextlib

        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task_b


async def test_wrong_session_cannot_authorize_tool_dispatch_even_when_owner_id_matches(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    """A same-org, BOTH-sessions-ACTIVE variant: the owner id matches and neither session
    is terminal, isolating the identity check from any terminal-state short-circuit —
    session A is simply not, and never was, turn B's parent session."""
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_tool(agent_stack, org.id, mock_http_server)
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.get",))
    worker = agent_stack.service

    session_a = await worker.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    session_b = await worker.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )

    agent_stack.script.append(FakeModelTurn(content="hang", hang_seconds=5.0))
    task_b, turn_b = await _claim_running_turn(worker, org.id, session_b.id)

    try:
        row_a = await worker.get_session(org.id, session_a.id)
        row_b = await worker.get_session(org.id, session_b.id)
        assert row_a.state.value == "ACTIVE"
        assert row_b.state.value == "ACTIVE"
        assert row_a.id != row_b.id

        with pytest.raises(AgentInvalidStateError):
            await worker._authorize_tool_dispatch(
                org.id, session_a.id, turn_b.id, turn_b.execution_owner_id, "crm.get", "deadbeef", 1
            )
        assert await _tool_permit_count(agent_stack, org.id, turn_b.id) == 0
        assert await _execs(agent_stack, org.id) == 0
    finally:
        task_b.cancel()
        import contextlib

        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task_b


async def test_stale_worker_with_random_session_id_cannot_forge_authority(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    """A random, never-existing session id must be rejected identically to a real but
    unrelated session — the check must be a positive identity PROOF, not merely
    "some session exists and is not terminal"."""
    org = await make_organization()
    principal = await make_tool_principal(org)
    agent = await _provision(agent_stack, org.id)
    worker = agent_stack.service

    session = await worker.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    agent_stack.script.append(FakeModelTurn(content="hang", hang_seconds=5.0))
    task, turn = await _claim_running_turn(worker, org.id, session.id)

    try:
        bogus_session_id = uuidlib.uuid7()
        with pytest.raises(AgentInvalidStateError):
            await worker._authorize_model_dispatch(
                org.id, bogus_session_id, turn.id, turn.execution_owner_id, 1, "fake-model"
            )
        assert await _model_permit_count_for_iteration(agent_stack, org.id, turn.id, 1) == 0
    finally:
        task.cancel()
        import contextlib

        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def test_duplicate_model_authorization_attempt_is_rejected_not_a_second_call(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    """The mandatory model duplicate-permit test (corrective #9 §10, INV-AUTH-ID-002).

    Unlike tool dispatch (backed by P08 idempotency), a model-provider call has no
    dedup layer of its own — this authorization boundary must be the ONLY thing
    preventing two real, independent, billed provider calls for the SAME iteration. A
    second attempt to authorize an iteration that already has a durable permit MUST be
    REJECTED (not silently treated as "also fine to proceed"), and the permit row count
    for that iteration MUST stay exactly 1 no matter how many attempts were made."""
    org = await make_organization()
    principal = await make_tool_principal(org)
    agent = await _provision(agent_stack, org.id)
    worker = agent_stack.service

    session = await worker.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    agent_stack.script.append(FakeModelTurn(content="hang", hang_seconds=5.0))
    task, turn = await _claim_running_turn(worker, org.id, session.id)

    try:
        # the FIRST attempt for this (turn, iteration) durably CREATES the permit —
        # this is the genuinely-authorized dispatch.
        await worker._authorize_model_dispatch(
            org.id, session.id, turn.id, turn.execution_owner_id, 50, "fake-model"
        )
        assert await _model_permit_count_for_iteration(agent_stack, org.id, turn.id, 50) == 1

        # a SECOND attempt for the IDENTICAL (turn, iteration) — same worker, same
        # owner, same session — is a duplicate. It MUST be rejected, not treated as a
        # second authorization to independently call the provider again.
        with pytest.raises(AgentInvalidStateError):
            await worker._authorize_model_dispatch(
                org.id, session.id, turn.id, turn.execution_owner_id, 50, "fake-model"
            )
        # the permit row count for THIS iteration is UNCHANGED — the rejected duplicate
        # created nothing (scoped to iteration 50, not a whole-turn total, since the
        # background task's own legitimate iteration-0 permit lands at a
        # nondeterministic point this test does not control).
        assert await _model_permit_count_for_iteration(agent_stack, org.id, turn.id, 50) == 1
    finally:
        task.cancel()
        import contextlib

        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def test_duplicate_tool_authorization_attempt_is_rejected_not_a_second_call(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    """SUPERSEDED by audit corrective #10 (INV-TOOL-PERMIT-001/002/003) — see
    ``tests/concurrency/test_agent_tool_permit_claim_ownership.py`` for the full
    dispatch-boundary certification (``AgentToolBridge.execute`` entry-count proof,
    two-worker proof). This test previously asserted tool dispatch's ALREADY_EXISTS
    outcome was safe to let proceed (P08's own idempotency key made a second
    ``AgentToolBridge.execute`` entry harmless). Corrective #10 determined that
    reasoning was correct about the EFFECT but insufficient for PERMIT-OWNERSHIP
    semantics — a caller receiving a bare "success" for a duplicate would still
    genuinely re-enter ``AgentToolBridge.execute`` / the Tool Engine, merely relying on
    P08 to make that entry harmless. Tool dispatch now REJECTS ALREADY_EXISTS
    identically to model dispatch: only the permit-creating attempt may proceed.
    Retained here (updated) as the corrective #9 regression file's own tool
    duplicate-permit test, so this file's own §11 requirement stays self-contained."""
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_tool(agent_stack, org.id, mock_http_server)
    seen: list[str] = []
    mock_http_server.set_handler(_handler(seen))
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.get",))
    worker = agent_stack.service

    session = await worker.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    agent_stack.script.append(FakeModelTurn(content="hang", hang_seconds=5.0))
    task, turn = await _claim_running_turn(worker, org.id, session.id)

    try:
        await worker._authorize_tool_dispatch(
            org.id,
            session.id,
            turn.id,
            turn.execution_owner_id,
            "crm.get",
            "deadbeefcafe",
            77,
        )
        assert await _tool_permit_count(agent_stack, org.id, turn.id) == 1

        with pytest.raises(AgentInvalidStateError):
            await worker._authorize_tool_dispatch(
                org.id,
                session.id,
                turn.id,
                turn.execution_owner_id,
                "crm.get",
                "deadbeefcafe",
                78,
            )
        # the permit row count is UNCHANGED — the rejected duplicate created nothing.
        assert await _tool_permit_count(agent_stack, org.id, turn.id) == 1
        # neither attempt actually dispatched anything itself (authorization is
        # distinct from execution) — the mock server was never hit.
        assert seen == []
        assert await _execs(agent_stack, org.id) == 0
    finally:
        task.cancel()
        import contextlib

        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def _handler(seen: list[str]) -> Any:
    def handle(method: str, path: str, headers: Any, body: Any) -> tuple[int, dict[str, Any]]:
        seen.append(path)
        return 200, {"id": path.rsplit("/", 1)[-1]}

    return handle


_IDENTITY_MATRIX_MODEL = [
    pytest.param("real", "real", "ok", id="model-correct-session-correct-owner-OK"),
    pytest.param("real", "wrong", "invalid_state", id="model-correct-session-wrong-owner-REJECT"),
    pytest.param("real", "none", "invalid_state", id="model-correct-session-no-owner-REJECT"),
    pytest.param(
        "wrong-live", "real", "invalid_state", id="model-wrong-live-session-correct-owner-REJECT"
    ),
    pytest.param("bogus", "real", "invalid_state", id="model-bogus-session-correct-owner-REJECT"),
    pytest.param(
        "wrong-live", "wrong", "invalid_state", id="model-wrong-session-wrong-owner-REJECT"
    ),
    pytest.param(
        "terminal", "real", "revoked", id="model-terminal-unrelated-session-correct-owner-REJECT"
    ),
]


@pytest.mark.parametrize("session_choice,owner_choice,expect", _IDENTITY_MATRIX_MODEL)
async def test_model_authority_identity_matrix(
    agent_stack: Any,
    make_organization: Any,
    make_tool_principal: Any,
    session_choice: str,
    owner_choice: str,
    expect: str,
) -> None:
    """Corrective #9 §15: the 7-row wrong-owner / wrong-session matrix for MODEL
    dispatch. Only the row where the session is turn B's OWN live parent AND the owner
    matches may succeed; every other combination — wrong owner, missing owner, a wholly
    unrelated live session, a nonexistent session, both wrong at once, or an unrelated
    but TERMINAL session — must be rejected."""
    org = await make_organization()
    principal = await make_tool_principal(org)
    agent = await _provision(agent_stack, org.id)
    worker = agent_stack.service

    session_b = await worker.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    session_a = await worker.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    session_c = await worker.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    await _cancel_session_row_directly(agent_stack, org.id, session_c.id)

    agent_stack.script.append(FakeModelTurn(content="hang", hang_seconds=5.0))
    task_b, turn_b = await _claim_running_turn(worker, org.id, session_b.id)

    try:
        session_id = {
            "real": session_b.id,
            "wrong-live": session_a.id,
            "bogus": uuidlib.uuid7(),
            "terminal": session_c.id,
        }[session_choice]
        owner_id = {
            "real": turn_b.execution_owner_id,
            "wrong": uuidlib.uuid7(),
            "none": None,
        }[owner_choice]

        if expect == "ok":
            await worker._authorize_model_dispatch(
                org.id, session_id, turn_b.id, owner_id, 1000, "fake-model"
            )
            assert (
                await _model_permit_count_for_iteration(agent_stack, org.id, turn_b.id, 1000) == 1
            )
        else:
            exc_type = AgentInvalidStateError if expect == "invalid_state" else ExecutionRevoked
            with pytest.raises(exc_type):
                await worker._authorize_model_dispatch(
                    org.id, session_id, turn_b.id, owner_id, 1000, "fake-model"
                )
            assert (
                await _model_permit_count_for_iteration(agent_stack, org.id, turn_b.id, 1000) == 0
            )
    finally:
        task_b.cancel()
        import contextlib

        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task_b


_IDENTITY_MATRIX_TOOL = [
    pytest.param("real", "real", "ok", id="tool-correct-session-correct-owner-OK"),
    pytest.param("real", "wrong", "invalid_state", id="tool-correct-session-wrong-owner-REJECT"),
    pytest.param("real", "none", "invalid_state", id="tool-correct-session-no-owner-REJECT"),
    pytest.param(
        "wrong-live", "real", "invalid_state", id="tool-wrong-live-session-correct-owner-REJECT"
    ),
    pytest.param("bogus", "real", "invalid_state", id="tool-bogus-session-correct-owner-REJECT"),
    pytest.param(
        "wrong-live", "wrong", "invalid_state", id="tool-wrong-session-wrong-owner-REJECT"
    ),
    pytest.param(
        "terminal", "real", "revoked", id="tool-terminal-unrelated-session-correct-owner-REJECT"
    ),
]


@pytest.mark.parametrize("session_choice,owner_choice,expect", _IDENTITY_MATRIX_TOOL)
async def test_tool_authority_identity_matrix(
    agent_stack: Any,
    make_organization: Any,
    make_tool_principal: Any,
    mock_http_server: Any,
    session_choice: str,
    owner_choice: str,
    expect: str,
) -> None:
    """Corrective #9 §15: the identical 7-row matrix for TOOL dispatch."""
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_tool(agent_stack, org.id, mock_http_server)
    mock_http_server.set_handler(_handler([]))
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.get",))
    worker = agent_stack.service

    session_b = await worker.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    session_a = await worker.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    session_c = await worker.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    await _cancel_session_row_directly(agent_stack, org.id, session_c.id)

    agent_stack.script.append(FakeModelTurn(content="hang", hang_seconds=5.0))
    task_b, turn_b = await _claim_running_turn(worker, org.id, session_b.id)

    try:
        session_id = {
            "real": session_b.id,
            "wrong-live": session_a.id,
            "bogus": uuidlib.uuid7(),
            "terminal": session_c.id,
        }[session_choice]
        owner_id = {
            "real": turn_b.execution_owner_id,
            "wrong": uuidlib.uuid7(),
            "none": None,
        }[owner_choice]

        if expect == "ok":
            await worker._authorize_tool_dispatch(
                org.id, session_id, turn_b.id, owner_id, "crm.get", "matrixhash", 1000
            )
            assert await _tool_permit_count(agent_stack, org.id, turn_b.id) == 1
        else:
            exc_type = AgentInvalidStateError if expect == "invalid_state" else ExecutionRevoked
            with pytest.raises(exc_type):
                await worker._authorize_tool_dispatch(
                    org.id, session_id, turn_b.id, owner_id, "crm.get", "matrixhash", 1000
                )
            assert await _tool_permit_count(agent_stack, org.id, turn_b.id) == 0
    finally:
        task_b.cancel()
        import contextlib

        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task_b


async def test_database_constraint_rejects_mismatched_session_turn_row_even_bypassing_python(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    """The mandatory direct-SQL database-constraint test (corrective #9 §13, §5's "Do
    NOT rely only on Python"). Runs a raw INSERT — through the SAME tenant-scoped,
    RLS-bound runtime transaction the application uses, but bypassing
    ``AgentService`` entirely — attempting to persist a permit row whose ``session_id``
    disagrees with its own ``turn_id``'s real parent session. A Python application bug
    that somehow skipped ``_assert_turn_authority`` entirely must STILL be unable to
    persist an inconsistent authorization record: this proves the composite FK
    (``fk_ai_agent_tool_dispatch_permits_org_session_turn`` /
    ``fk_ai_agent_model_dispatch_permits_org_session_turn``, migration ``b4c5d6e7f8a9``)
    rejects it at the DATABASE level, independent of any application code path."""
    import uuid as _uuid

    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    org = await make_organization()
    principal = await make_tool_principal(org)
    agent = await _provision(agent_stack, org.id)
    worker = agent_stack.service

    session_a = await worker.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    session_b = await worker.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    agent_stack.script.append(FakeModelTurn(content="hang", hang_seconds=5.0))
    task_b, turn_b = await _claim_running_turn(worker, org.id, session_b.id)

    try:
        # attempt a raw INSERT: turn_id genuinely belongs to session_b, but the row
        # under construction CLAIMS session_a — the exact mismatch corrective #9
        # closes. Must fail with a foreign-key violation, not merely "be discouraged".
        with pytest.raises(IntegrityError):
            async with agent_stack.database.tenant_transaction(org.id) as tenant:
                await tenant.session.execute(
                    text(
                        "INSERT INTO ai_agent_tool_dispatch_permits "
                        "(id, organization_id, session_id, turn_id, sequence, tool_key, "
                        "arguments_hash) VALUES "
                        "(:id, :org, :sess, :turn, :seq, :tool, :hash)"
                    ),
                    {
                        "id": str(_uuid.uuid4()),
                        "org": str(org.id),
                        "sess": str(session_a.id),  # WRONG — turn_b belongs to session_b
                        "turn": str(turn_b.id),
                        "seq": 9999,
                        "tool": "crm.get",
                        "hash": "directsql",
                    },
                )

        with pytest.raises(IntegrityError):
            async with agent_stack.database.tenant_transaction(org.id) as tenant:
                await tenant.session.execute(
                    text(
                        "INSERT INTO ai_agent_model_dispatch_permits "
                        "(id, organization_id, session_id, turn_id, iteration, model) "
                        "VALUES (:id, :org, :sess, :turn, :it, :model)"
                    ),
                    {
                        "id": str(_uuid.uuid4()),
                        "org": str(org.id),
                        "sess": str(session_a.id),  # WRONG — turn_b belongs to session_b
                        "turn": str(turn_b.id),
                        "it": 9999,
                        "model": "fake-model",
                    },
                )

        # neither malformed row was ever persisted (scoped to the specific
        # sequence/iteration this test used — 9999 — since the background task's own
        # legitimate iteration-0 model permit lands at a nondeterministic point this
        # test does not control; the tool table has no such background activity at all,
        # so a whole-turn total is already exact there).
        assert await _tool_permit_count(agent_stack, org.id, turn_b.id) == 0
        assert await _model_permit_count_for_iteration(agent_stack, org.id, turn_b.id, 9999) == 0
    finally:
        task_b.cancel()
        import contextlib

        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task_b
