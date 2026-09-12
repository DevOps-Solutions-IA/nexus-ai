"""NXS-P13 audit corrective #3 — terminal-state absorption at the DATABASE boundary and
the absolute session lifetime during an in-flight turn.

Fail-first against 6550d12: `_finish_turn` re-read the session `FOR UPDATE` but then
unconditionally wrote `state = ACTIVE`, so a session another worker had terminalised
`EXPIRED` could be resurrected `EXPIRED -> ACTIVE`; and `max_session_seconds` was only a
new-turn admission check, so a turn opened just before expiry ran models / tools past the
absolute ceiling.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.agents.entities import StartAgentSessionRequest, SubmitTurnRequest
from nexus_ai.agents.errors import AgentSessionExpiredError
from nexus_ai.agents.models.fake import FakeModelProvider, FakeModelTurn
from nexus_ai.agents.service import AgentService
from nexus_ai.agents.state_machine import AgentSessionState
from nexus_ai.integrations.destination import DestinationPolicy
from tests.integration.test_agent_service import _provision, _register_tool

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

_LOOPBACK = DestinationPolicy(allow_loopback=True, resolver=lambda h, p: ["127.0.0.1"])


class _NoHttp:
    async def request(self, **_kw: Any) -> Any:  # pragma: no cover - fake never calls it
        raise AssertionError("the fake model provider must not use the HTTP transport")


def _second_service(stack: Any) -> tuple[AgentService, FakeModelProvider]:
    """An INDEPENDENT AgentService (own process-local locks + fake provider) sharing the
    SAME PostgreSQL database, event outbox, vault and Tool Engine as ``stack.service``."""
    provider = FakeModelProvider()
    service = AgentService(
        stack.settings,
        stack.database,
        stack.event_platform.publisher,
        stack.vault,
        stack.tool_engine,
        stack.tool_registry,
        _NoHttp(),
        destination_policy=_LOOPBACK,
        model_provider_factory=lambda _p: provider,
    )
    return service, provider


def _compress(svc: AgentService, **overrides: float) -> None:
    svc._cfg = svc._cfg.model_copy(update=overrides)


async def _session_row(stack: Any, org_id: Any, session_id: Any) -> Any:
    async with stack.database.tenant_transaction(org_id) as tenant:
        return (
            await tenant.session.execute(
                text(
                    "SELECT state, error_code, ended_at, response_text_present "
                    "FROM ("
                    "  SELECT s.state, s.error_code, s.ended_at, "
                    "  EXISTS(SELECT 1 FROM ai_agent_turns t "
                    "         WHERE t.session_id = s.id AND t.response_text IS NOT NULL) "
                    "  AS response_text_present "
                    "  FROM ai_agent_sessions s WHERE s.id = :sid"
                    ") x"
                ),
                {"sid": str(session_id)},
            )
        ).one()


async def _agent_events(stack: Any, org_id: Any, like: str) -> list[str]:
    async with stack.database.tenant_transaction(org_id) as tenant:
        return [
            r[0]
            for r in (
                await tenant.session.execute(
                    text("SELECT event_type FROM event_outbox WHERE event_type LIKE :p"),
                    {"p": like},
                )
            ).all()
        ]


async def test_two_services_cannot_resurrect_an_expired_session(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_tool(agent_stack, org.id, mock_http_server)
    mock_http_server.set_handler(lambda m, p, h, b: (200, {"id": "x"}))

    worker_a = agent_stack.service  # default cfg: max_session_seconds 3600, deadline 120
    worker_b, provider_b = _second_service(agent_stack)
    _compress(worker_b, max_session_seconds=1.0)

    agent = await _provision(agent_stack, org.id, tool_keys=("crm.get",))
    session = await worker_a.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )

    # Worker A opens a turn and blocks in the model for ~3s (well under its own 120s
    # deadline and 3600s lifetime — A alone would never expire this session).
    agent_stack.script.append(FakeModelTurn(content="A's late answer", hang_seconds=3.0))
    task_a = asyncio.create_task(
        worker_a.submit_turn(org.id, session.id, SubmitTurnRequest(content="a"))
    )
    await asyncio.sleep(0.2)

    # meanwhile the absolute lifetime (per Worker B's config) passes and Worker B, on a
    # new-turn attempt, transitions the shared row to EXPIRED.
    await asyncio.sleep(1.0)
    provider_b.script.append(FakeModelTurn(content="never runs"))
    with pytest.raises(AgentSessionExpiredError):
        await worker_b.submit_turn(org.id, session.id, SubmitTurnRequest(content="b"))

    row = await _session_row(agent_stack, org.id, session.id)
    assert row.state == "EXPIRED" and row.error_code == "NXS_AGENT_SESSION_EXPIRED"
    expired_ended_at = row.ended_at
    assert expired_ended_at is not None

    # release Worker A: its model finishes and it enters _finish_turn, locks the row, and
    # MUST observe the terminal state — no EXPIRED -> ACTIVE.
    with pytest.raises((AgentSessionExpiredError, Exception)):
        await task_a

    row = await _session_row(agent_stack, org.id, session.id)
    assert row.state == "EXPIRED"  # never resurrected
    assert row.error_code == "NXS_AGENT_SESSION_EXPIRED"
    assert row.ended_at == expired_ended_at  # untouched
    assert row.response_text_present is False  # A's stale answer was never committed

    events = await _agent_events(agent_stack, org.id, "agent.%")
    assert "agent.session.expired" in events
    # exactly one response.ready would be wrong; A's turn produced none
    assert events.count("agent.response.ready") == 0

    async with agent_stack.database.tenant_transaction(org.id) as tenant:
        turn_states = [
            r[0]
            for r in (
                await tenant.session.execute(
                    text("SELECT state FROM ai_agent_turns WHERE session_id = :s"),
                    {"s": str(session.id)},
                )
            ).all()
        ]
    assert turn_states == ["CANCELLED"]  # A's turn was discarded, not completed

    await worker_b.shutdown()


async def test_expiry_during_the_model_call_yields_session_expired_not_turn_timeout(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    _compress(agent_stack.service, max_session_seconds=1.0, turn_deadline_seconds=30.0)
    agent = await _provision(agent_stack, org.id)
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    # ~0.9s used, so the turn opens with ~0.1s of session lifetime left; the model hangs 1s.
    await asyncio.sleep(0.9)
    agent_stack.script.append(FakeModelTurn(content="too slow", hang_seconds=1.0))

    with pytest.raises(AgentSessionExpiredError):
        await agent_stack.service.submit_turn(org.id, session.id, SubmitTurnRequest(content="q"))

    row = await _session_row(agent_stack, org.id, session.id)
    assert row.state == "EXPIRED" and row.error_code == "NXS_AGENT_SESSION_EXPIRED"
    assert row.response_text_present is False
    turns = await agent_stack.service.list_turns(org.id, session.id, limit=5)
    assert turns[0].state.value == "CANCELLED"
    assert turns[0].error_code == "NXS_AGENT_SESSION_EXPIRED"  # not TURN / PROVIDER timeout


async def test_expiry_during_the_tool_loop_stops_further_tool_calls(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    principal = await make_tool_principal(org)
    await _register_tool(agent_stack, org.id, mock_http_server)
    mock_http_server.set_handler(lambda m, p, h, b: (200, {"id": "x"}))
    _compress(agent_stack.service, max_session_seconds=1.2, turn_deadline_seconds=30.0)
    agent = await _provision(agent_stack, org.id, tool_keys=("crm.get",))
    session = await agent_stack.service.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    # iteration 0 asks for one tool (executes fast); iteration 1's model call hangs past
    # the absolute lifetime.
    agent_stack.script.append(
        FakeModelTurn(tool_calls=(("crm.get", {"path_params": {"id": "x"}}),))
    )
    agent_stack.script.append(FakeModelTurn(content="continuation", hang_seconds=5.0))

    with pytest.raises(AgentSessionExpiredError):
        await agent_stack.service.submit_turn(org.id, session.id, SubmitTurnRequest(content="q"))

    async with agent_stack.database.tenant_transaction(org.id) as tenant:
        execs = (
            await tenant.session.execute(text("SELECT count(*) FROM tool_execution_records"))
        ).scalar_one()
    assert execs == 1  # the one before expiry; no late tool call after the deadline
    row = await _session_row(agent_stack, org.id, session.id)
    assert row.state == "EXPIRED"


async def test_finish_turn_never_overwrites_a_cancelled_session(
    agent_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    """A stop/cancel that lands while a turn is finishing must also stay absorbing."""
    org = await make_organization()
    principal = await make_tool_principal(org)
    worker_a = agent_stack.service
    worker_b, _pb = _second_service(agent_stack)
    agent = await _provision(agent_stack, org.id)
    session = await worker_a.start_session(
        org.id, principal, StartAgentSessionRequest(agent_id=agent.id)
    )
    agent_stack.script.append(FakeModelTurn(content="A answer", hang_seconds=1.5))
    task_a = asyncio.create_task(
        worker_a.submit_turn(org.id, session.id, SubmitTurnRequest(content="a"))
    )
    await asyncio.sleep(0.2)
    from nexus_ai.agents.entities import StopAgentSessionRequest

    stopped = await worker_b.stop_session(org.id, session.id, StopAgentSessionRequest())
    assert stopped.state is AgentSessionState.COMPLETED

    with pytest.raises(Exception):  # noqa: B017
        await task_a
    row = await _session_row(agent_stack, org.id, session.id)
    assert row.state == "COMPLETED"  # not resurrected to ACTIVE
    assert row.response_text_present is False
    await worker_b.shutdown()
