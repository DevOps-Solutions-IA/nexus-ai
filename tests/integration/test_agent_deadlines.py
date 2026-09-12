"""NXS-P13 audit corrective #2 — per-Agent turn deadline, absolute session lifetime,
and the stable whole-turn timeout taxonomy. Real DB + event outbox + Tool Engine.

Fail-first against feaefaf: ``agent.timeout_seconds`` was dead config, ``max_session_seconds``
was never enforced on an ACTIVE session, and the total-turn deadline re-raised a raw
``TimeoutError`` instead of a stable ``NXS_AGENT_*`` error.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.agents.entities import (
    AgentChannel,
    CreateAgentRequest,
    StartAgentSessionRequest,
    SubmitTurnRequest,
)
from nexus_ai.agents.errors import (
    AgentInvalidStateError,
    AgentSessionExpiredError,
    AgentTurnTimeoutError,
)
from nexus_ai.agents.models.fake import FakeModelTurn
from nexus_ai.agents.state_machine import AgentSessionState
from tests.integration.test_agent_service import _provision, _register_tool

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


def _compress(stack: Any, **overrides: float) -> None:
    """Shrink the global runtime ceilings so deadline tests run in milliseconds."""
    stack.service._cfg = stack.service._cfg.model_copy(update=overrides)


async def _agent(stack: Any, org_id: Any, **kw: Any) -> Any:
    profile_id = (await _provision(stack, org_id)).model_profile_id
    return await stack.service.create_agent(
        org_id,
        CreateAgentRequest(
            slug=f"a-{uuid.uuid4().hex[:8]}",
            display_name="A",
            model_profile_id=profile_id,
            system_instructions="be brief",
            tool_keys=(),
            **kw,
        ),
    )


async def _tool_execs(stack: Any, org_id: Any) -> int:
    async with stack.database.tenant_transaction(org_id) as tenant:
        return int(
            (
                await tenant.session.execute(text("SELECT count(*) FROM tool_execution_records"))
            ).scalar_one()
        )


# ---------------------------------------------------------------- Blocker 1


async def test_effective_turn_deadline_never_widens_the_global_ceiling(
    agent_stack: Any,
) -> None:
    svc = agent_stack.service
    ceiling = svc._cfg.turn_deadline_seconds

    class _A:
        timeout_seconds: float | None

    tight = _A()
    tight.timeout_seconds = 1.0
    wide = _A()
    wide.timeout_seconds = min(600.0, ceiling + 300.0)
    none = _A()
    none.timeout_seconds = None

    assert svc._effective_turn_deadline(tight) == 1.0  # tenant tightens
    assert svc._effective_turn_deadline(wide) == ceiling  # tenant cannot widen
    assert svc._effective_turn_deadline(none) == ceiling  # null -> global fallback


@pytest.mark.parametrize(
    ("global_deadline", "agent_timeout", "expect_near"),
    [
        (120.0, 1.0, 1.0),  # agent tightens a wide global
        (1.0, 30.0, 1.0),  # agent value is capped at the global ceiling
        (1.0, None, 1.0),  # null agent value -> global
    ],
)
async def test_turn_times_out_at_the_effective_deadline(
    agent_stack: Any,
    make_organization: Any,
    make_tool_principal: Any,
    global_deadline: float,
    agent_timeout: float | None,
    expect_near: float,
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    _compress(agent_stack, turn_deadline_seconds=global_deadline)
    agent = await _agent(agent_stack, org.id, timeout_seconds=agent_timeout)
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    agent_stack.script.append(FakeModelTurn(content="late answer", hang_seconds=5.0))

    started = time.monotonic()
    with pytest.raises(AgentTurnTimeoutError):
        await agent_stack.service.submit_turn(org.id, session.id, SubmitTurnRequest(content="q"))
    elapsed = time.monotonic() - started
    assert expect_near - 0.3 <= elapsed <= expect_near + 1.5

    turns = await agent_stack.service.list_turns(org.id, session.id, limit=5)
    assert turns[0].state.value == "FAILED"
    assert turns[0].error_code == "NXS_AGENT_TURN_TIMEOUT"
    assert turns[0].response_text is None  # the stale model answer is never committed
    # the session survives a timed-out turn and is usable again
    assert (
        await agent_stack.service.get_session(org.id, session.id)
    ).state is AgentSessionState.ACTIVE


async def test_turn_timeout_raises_the_stable_error_never_raw_timeout(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    _compress(agent_stack, turn_deadline_seconds=0.5)
    agent = await _agent(agent_stack, org.id)
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    agent_stack.script.append(FakeModelTurn(content="x", hang_seconds=5.0))

    with pytest.raises(AgentTurnTimeoutError) as excinfo:
        await agent_stack.service.submit_turn(org.id, session.id, SubmitTurnRequest(content="q"))
    exc = excinfo.value
    assert exc.code == "NXS_AGENT_TURN_TIMEOUT" and exc.status == 504
    assert not isinstance(exc, TimeoutError)  # not the builtin
    # no in-flight task leaked
    assert session.id not in agent_stack.service._turn_tasks

    events = [r[0] async for r in _iter_events(agent_stack, org.id)]
    assert "agent.turn.failed" in events


async def _iter_events(stack: Any, org_id: Any) -> Any:
    async with stack.database.tenant_transaction(org_id) as tenant:
        rows = (
            await tenant.session.execute(
                text("SELECT event_type FROM event_outbox WHERE event_type LIKE 'agent.%'")
            )
        ).all()
    for row in rows:
        yield row


async def test_a_fast_turn_under_the_agent_deadline_still_completes(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    _compress(agent_stack, turn_deadline_seconds=10.0)
    agent = await _agent(agent_stack, org.id, timeout_seconds=None)
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    agent_stack.script.append(FakeModelTurn(content="quick"))
    response = await agent_stack.service.submit_turn(
        org.id, session.id, SubmitTurnRequest(content="q")
    )
    assert response.content == "quick"


# ---------------------------------------------------------------- Blocker 2


async def test_lifetime_exceeded_boundary_is_inclusive(agent_stack: Any) -> None:
    from nexus_ai.agents.entities import AgentSession

    _compress(agent_stack, max_session_seconds=2.0)
    started = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    session = AgentSession.model_construct(started_at=started)
    check = agent_stack.service._lifetime_exceeded
    assert check(session, started + dt.timedelta(seconds=1.999)) is False
    assert check(session, started + dt.timedelta(seconds=2.0)) is True
    assert check(session, started + dt.timedelta(seconds=2.001)) is True


async def test_expired_session_rejects_a_new_turn_before_any_model_call(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_tool(agent_stack, org.id, mock_http_server)
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.get",))
    _compress(agent_stack, max_session_seconds=1.0)
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    agent_stack.script.append(FakeModelTurn(content="should never run"))
    await asyncio.sleep(1.1)

    with pytest.raises(AgentSessionExpiredError):
        await agent_stack.service.submit_turn(
            org.id, session.id, SubmitTurnRequest(content="too late")
        )

    assert len(agent_stack.provider.calls) == 0  # no model provider call
    assert await _tool_execs(agent_stack, org.id) == 0  # no Tool Engine execution

    ended = await agent_stack.service.get_session(org.id, session.id)
    assert ended.state is AgentSessionState.EXPIRED
    assert ended.ended_at is not None
    assert ended.error_code == "NXS_AGENT_SESSION_EXPIRED"

    async with agent_stack.database.tenant_transaction(org.id) as tenant:
        events = (
            await tenant.session.execute(
                text(
                    "SELECT event_type FROM event_outbox WHERE event_type = 'agent.session.expired'"
                )
            )
        ).all()
        turn_rows = (
            await tenant.session.execute(text("SELECT count(*) FROM ai_agent_turns"))
        ).scalar_one()
    assert len(events) == 1
    assert turn_rows == 0  # no turn row was opened

    # cannot be resurrected — a later submit is an invalid-state rejection
    with pytest.raises(AgentInvalidStateError):
        await agent_stack.service.submit_turn(
            org.id, session.id, SubmitTurnRequest(content="again")
        )


async def test_session_lifetime_boundary_accepts_then_rejects(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    _compress(agent_stack, max_session_seconds=2.0)
    agent = await _provision(agent_stack, org.id)
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    agent_stack.script.append(FakeModelTurn(content="in time"))
    early = await agent_stack.service.submit_turn(
        org.id, session.id, SubmitTurnRequest(content="q1")
    )
    assert early.content == "in time"

    await asyncio.sleep(2.1)
    with pytest.raises(AgentSessionExpiredError):
        await agent_stack.service.submit_turn(org.id, session.id, SubmitTurnRequest(content="q2"))


async def test_concurrent_expiry_and_submit_allows_no_late_execution(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    _compress(agent_stack, max_session_seconds=1.0)
    agent = await _provision(agent_stack, org.id)
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    agent_stack.script.append(FakeModelTurn(content="never"))
    agent_stack.script.append(FakeModelTurn(content="never either"))
    await asyncio.sleep(1.1)

    results = await asyncio.gather(
        agent_stack.service.submit_turn(org.id, session.id, SubmitTurnRequest(content="a")),
        agent_stack.service.submit_turn(org.id, session.id, SubmitTurnRequest(content="b")),
        return_exceptions=True,
    )
    assert all(isinstance(r, BaseException) for r in results)
    assert any(isinstance(r, AgentSessionExpiredError) for r in results)
    assert len(agent_stack.provider.calls) == 0  # not one late model call


async def test_start_session_with_expired_channel_metadata_is_unaffected(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    # sanity: a brand-new session is never immediately expired
    org = await make_organization()
    principal = await make_tool_principal(org)
    agent = await _provision(agent_stack, org.id)
    session = await agent_stack.service.start_session(
        org.id,
        principal,
        StartAgentSessionRequest(agent_id=agent.id, channel=AgentChannel.API),
    )
    agent_stack.script.append(FakeModelTurn(content="ok"))
    assert (
        await agent_stack.service.submit_turn(org.id, session.id, SubmitTurnRequest(content="q"))
    ).content == "ok"
