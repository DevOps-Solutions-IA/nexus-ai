"""Placement admission composes with real domain transactions, not API-only checks."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from nexus_ai.agents.entities import StartAgentSessionRequest, SubmitTurnRequest
from nexus_ai.agents.idempotency import turn_fingerprint
from nexus_ai.agents.models.fake import FakeModelTurn
from nexus_ai.cells.admission import placement_route
from nexus_ai.cells.contracts import PlacementMutation
from nexus_ai.cells.errors import PlacementFencedError, PlacementRequiredError
from nexus_ai.domain.agents.repository import AgentModelDispatchPermitRepository
from nexus_ai.humans.entities import (
    AssignmentActionRequest,
    ClaimWorkRequest,
    HumanSendRequest,
    ReturnToAiRequest,
)
from nexus_ai.workflows.entities import StartWorkflowRunRequest
from tests.integration.test_agent_service import _provision
from tests.integration.test_campaign_service import ready_claim, running_campaign
from tests.integration.test_cell_placement import _assign
from tests.integration.test_cell_placement import placement_setup as placement_setup
from tests.integration.test_human_operations_service import _claimed, _ready_work
from tests.integration.test_scheduler_service import _request, _version
from tests.integration.test_workflow_service import _noop, _published

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _transition(setup: Any, operation: str, generation: int) -> Any:
    organization, actor, service, cell_id = setup
    return await service.mutate(
        organization.id,
        actor.user_id,
        PlacementMutation(
            cell_id=cell_id,
            operation=operation,
            expected_generation=generation,
            idempotency_key=uuid4().hex,
            reason_code=operation,
        ),
        uuid4(),
    )


async def test_rollout_is_explicit_and_legacy_worker_cannot_serve_placed_org(
    placement_setup: Any, tenant_database: Any, monkeypatch: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    async with tenant_database.execution_transaction(organization.id):
        pass
    monkeypatch.setattr(tenant_database, "_worker_cell_id", cell_id)
    with pytest.raises(PlacementRequiredError):
        async with tenant_database.execution_transaction(organization.id):
            pytest.fail("unassigned organization admitted")
    await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    async with tenant_database.execution_transaction(organization.id):
        pass
    monkeypatch.setattr(tenant_database, "_worker_cell_id", None)
    with pytest.raises(PlacementFencedError):
        async with tenant_database.execution_transaction(organization.id):
            pytest.fail("legacy worker admitted a placed organization")


async def test_p13_placement_preserves_session_and_turn_replay(
    placement_setup: Any, agent_stack: Any, monkeypatch: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    monkeypatch.setattr(agent_stack.database, "_worker_cell_id", cell_id)
    first = await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    agent = await _provision(agent_stack, organization.id)
    session_request = StartAgentSessionRequest(agent_id=agent.id, idempotency_key="cell-session")
    session = await agent_stack.service.start_session(organization.id, actor, session_request)
    agent_stack.push(FakeModelTurn(content="placed"))
    turn_request = SubmitTurnRequest(content="hello", idempotency_key="cell-turn")
    response = await agent_stack.service.submit_turn(organization.id, session.id, turn_request)
    replay = await agent_stack.service.submit_turn(organization.id, session.id, turn_request)
    assert replay == response
    assert len(agent_stack.provider.calls) == 1
    await _transition(placement_setup, "SUSPEND", 1)
    assert (
        await agent_stack.service.start_session(organization.id, actor, session_request)
    ).id == session.id
    with pytest.raises(PlacementFencedError):
        await agent_stack.service.start_session(
            organization.id,
            actor,
            session_request.model_copy(update={"idempotency_key": "cell-session-new"}),
        )
    with pytest.raises(PlacementFencedError):
        await agent_stack.service.submit_turn(
            organization.id, session.id, SubmitTurnRequest(content="blocked")
        )
    await _transition(placement_setup, "RESUME", 2)
    with placement_route(first), pytest.raises(PlacementFencedError):
        await agent_stack.service.submit_turn(organization.id, session.id, turn_request)
    assert (
        await agent_stack.service.start_session(organization.id, actor, session_request)
    ).id == session.id
    assert len(agent_stack.provider.calls) == 1


async def test_p17_claim_fenced_without_changing_ownership_or_capacity(
    placement_setup: Any, human_stack: Any, monkeypatch: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    monkeypatch.setattr(human_stack.database, "_worker_cell_id", cell_id)
    first = await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    queue, work, conversation = await _ready_work(human_stack, organization, actor)
    request = ClaimWorkRequest(queue_id=queue.id, expected_queue_revision=queue.revision)
    before = await human_stack.service.get_ownership(organization.id, conversation.id)
    await _transition(placement_setup, "SUSPEND", 1)
    with pytest.raises(PlacementFencedError):
        await human_stack.service.claim_work(organization.id, actor.user_id, request)
    assert await human_stack.service.get_ownership(organization.id, conversation.id) == before
    await _transition(placement_setup, "RESUME", 2)
    with placement_route(first), pytest.raises(PlacementFencedError):
        await human_stack.service.claim_work(organization.id, actor.user_id, request)
    claim = await human_stack.service.claim_work(organization.id, actor.user_id, request)
    assert claim.work_item.id == work.id
    async with human_stack.database.tenant_transaction(organization.id) as tenant:
        assert (
            await tenant.session.execute(text("SELECT count(*) FROM human_assignments"))
        ).scalar_one() == 1


async def test_workflow_route_retry_keeps_exact_run_and_step_identity(
    placement_setup: Any, workflow_stack: Any, monkeypatch: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    monkeypatch.setattr(workflow_stack.database, "_worker_cell_id", cell_id)
    first = await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    _, version = await _published(workflow_stack, organization.id, (_noop("one"),))
    request = StartWorkflowRunRequest(
        workflow_version_id=version.id, idempotency_key="cell-workflow"
    )
    run = await workflow_stack.service.start_run(organization.id, request)
    assert (await workflow_stack.service.start_run(organization.id, request)).id == run.id
    await _transition(placement_setup, "SUSPEND", 1)
    with pytest.raises(PlacementFencedError):
        await workflow_stack.service.claim_next(organization.id, run.id)
    await _transition(placement_setup, "RESUME", 2)
    with placement_route(first), pytest.raises(PlacementFencedError):
        await workflow_stack.service.claim_next(organization.id, run.id)
    claim = await workflow_stack.service.claim_next(organization.id, run.id)
    assert claim is not None
    assert await workflow_stack.service.claim_next(organization.id, run.id) is None
    assert len(await workflow_stack.service.list_steps(organization.id, run.id)) == 1


async def test_scheduler_route_retry_preserves_occurrence_and_p14_key(
    placement_setup: Any, scheduler_stack: Any, monkeypatch: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    monkeypatch.setattr(scheduler_stack.database, "_worker_cell_id", cell_id)
    await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    version = await _version(scheduler_stack, organization.id)
    schedule = await scheduler_stack.service.create_schedule(organization.id, _request(version.id))
    await scheduler_stack.service.activate_schedule(organization.id, schedule.id)
    occurrences = await scheduler_stack.service.materialize_due(organization.id)
    await _transition(placement_setup, "SUSPEND", 1)
    with pytest.raises(PlacementFencedError):
        await scheduler_stack.service.claim_due(organization.id, uuid4())
    await _transition(placement_setup, "RESUME", 2)
    claim = await scheduler_stack.service.claim_due(organization.id, uuid4())
    assert claim.occurrence.id == occurrences[0].id
    dispatched = await scheduler_stack.service.dispatch_claim(organization.id, claim)
    replay = await scheduler_stack.workflows.start_run(
        organization.id,
        StartWorkflowRunRequest(
            workflow_version_id=version.id,
            input=claim.workflow_input,
            idempotency_key=claim.occurrence.p14_idempotency_key,
        ),
    )
    assert replay.id == dispatched.workflow_run_id
    assert await scheduler_stack.service.materialize_due(organization.id) == []


async def test_campaign_stale_route_cannot_authorize_or_duplicate_send(
    placement_setup: Any, campaign_stack: Any, monkeypatch: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    monkeypatch.setattr(campaign_stack.database, "_worker_cell_id", cell_id)
    first = await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    _, run, _ = await running_campaign(campaign_stack, organization.id)
    claim = await ready_claim(campaign_stack, organization.id, run)
    await _transition(placement_setup, "SUSPEND", 1)
    with pytest.raises(PlacementFencedError):
        await campaign_stack.service.authorize_send(organization.id, claim)
    await _transition(placement_setup, "RESUME", 2)
    with placement_route(first), pytest.raises(PlacementFencedError):
        await campaign_stack.service.authorize_send(organization.id, claim)
    assert campaign_stack.messaging.transport.requests == []
    permit = await campaign_stack.service.authorize_send(organization.id, claim)
    first_send = await campaign_stack.service.dispatch_send(organization.id, permit.id)
    replay = await campaign_stack.service.dispatch_send(organization.id, permit.id)
    assert first_send.message_id == replay.message_id
    assert len(campaign_stack.messaging.transport.requests) == 1


async def test_p13_model_permit_holds_placement_lock_in_same_transaction(
    placement_setup: Any, agent_stack: Any, monkeypatch: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    monkeypatch.setattr(agent_stack.database, "_worker_cell_id", cell_id)
    await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    agent = await _provision(agent_stack, organization.id)
    session = await agent_stack.service.start_session(
        organization.id, actor, StartAgentSessionRequest(agent_id=agent.id)
    )
    request = SubmitTurnRequest(content="permit", idempotency_key="permit-once")
    turn, _, owner = await agent_stack.service._open_turn(
        organization.id,
        session.id,
        request,
        turn_fingerprint(session_id=session.id, content=request.content),
    )
    assert owner
    inserted = asyncio.Event()
    finish = asyncio.Event()
    original = AgentModelDispatchPermitRepository.insert

    async def held_insert(repo: Any, values: Any) -> Any:
        result = await original(repo, values)
        inserted.set()
        await finish.wait()
        return result

    monkeypatch.setattr(AgentModelDispatchPermitRepository, "insert", held_insert)
    task = asyncio.create_task(
        agent_stack.service._authorize_model_dispatch(
            organization.id, session.id, turn.id, turn.execution_owner_id, 0, "fake-model"
        )
    )
    try:
        async with asyncio.timeout(10):
            await inserted.wait()
            async with agent_stack.database.tenant_transaction(organization.id) as contender:
                with pytest.raises(DBAPIError) as failure:
                    async with contender.session.begin_nested():
                        await contender.session.execute(
                            text("SELECT id FROM organization_placements FOR UPDATE NOWAIT")
                        )
                assert failure.value.orig.sqlstate == "55P03"
            finish.set()
            await task
    finally:
        finish.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    await _transition(placement_setup, "SUSPEND", 1)
    with pytest.raises(PlacementFencedError):
        await agent_stack.service._authorize_model_dispatch(
            organization.id, session.id, turn.id, turn.execution_owner_id, 1, "fake-model"
        )
    with pytest.raises(PlacementFencedError):
        await agent_stack.service._authorize_tool_dispatch(
            organization.id, session.id, turn.id, turn.execution_owner_id, "crm.get", "a" * 64, 0
        )
    async with agent_stack.database.tenant_transaction(organization.id) as tenant:
        assert (
            await tenant.session.execute(
                text("SELECT count(*) FROM ai_agent_model_dispatch_permits")
            )
        ).scalar_one() == 1
        assert (
            await tenant.session.execute(
                text("SELECT count(*) FROM ai_agent_tool_dispatch_permits")
            )
        ).scalar_one() == 0
    assert agent_stack.provider.calls == []


async def test_p17_ai_return_cannot_grant_ownership_after_placement_suspends(
    placement_setup: Any, human_stack: Any, monkeypatch: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    monkeypatch.setattr(human_stack.database, "_worker_cell_id", cell_id)
    await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    claim, conversation = await _claimed(human_stack, organization, actor)
    await human_stack.service.accept_assignment(
        organization.id,
        claim.assignment.id,
        AssignmentActionRequest(
            claim_token=claim.claim_token,
            lease_version=claim.assignment.lease_version,
            ownership_generation=claim.ownership_generation,
        ),
        actor_user_id=actor.user_id,
    )
    ownership = await human_stack.service.get_ownership(organization.id, conversation.id)
    agent = await _provision(human_stack.agents, organization.id)
    request = ReturnToAiRequest(
        claim_token=claim.claim_token,
        lease_version=claim.assignment.lease_version,
        ownership_generation=ownership.ownership_generation,
        agent_id=agent.id,
        idempotency_key="placement-ai-return",
        reason_code="RETURN_TO_AI",
        context="Continue with the same governed execution.",
    )
    original = human_stack.agents.service.start_session
    accepted_ids: list[Any] = []

    async def suspend_after_accept(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        accepted_ids.append(result.id)
        await _transition(placement_setup, "SUSPEND", 1)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(human_stack.agents.service, "start_session", suspend_after_accept)
        with pytest.raises(PlacementFencedError):
            await human_stack.service.return_to_ai(
                organization.id, claim.assignment.id, request, principal=actor
            )
    pending = await human_stack.service.get_ownership(organization.id, conversation.id)
    assert pending.mode.value == "UNASSIGNED" and pending.ai_session_id is None
    assert pending.ownership_generation == ownership.ownership_generation + 1
    await _transition(placement_setup, "RESUME", 2)
    bound = await human_stack.service.return_to_ai(
        organization.id, claim.assignment.id, request, principal=actor
    )
    assert bound.ai_session_id == accepted_ids[0]
    assert bound.ownership_generation == pending.ownership_generation + 1
    replay = await human_stack.service.return_to_ai(
        organization.id, claim.assignment.id, request, principal=actor
    )
    assert replay == bound


async def test_p17_send_cannot_authorize_from_suspended_cell(
    placement_setup: Any, human_stack: Any, monkeypatch: Any
) -> None:
    from tests.integration.test_campaign_service import campaign_account

    organization, actor, service, cell_id = placement_setup
    monkeypatch.setattr(human_stack.database, "_worker_cell_id", cell_id)
    await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    claim, conversation = await _claimed(human_stack, organization, actor)
    await human_stack.service.accept_assignment(
        organization.id,
        claim.assignment.id,
        AssignmentActionRequest(
            claim_token=claim.claim_token,
            lease_version=claim.assignment.lease_version,
            ownership_generation=claim.ownership_generation,
        ),
        actor_user_id=actor.user_id,
    )
    ownership = await human_stack.service.get_ownership(organization.id, conversation.id)
    account = await campaign_account(human_stack, organization.id)
    request = HumanSendRequest(
        claim_token=claim.claim_token,
        lease_version=claim.assignment.lease_version,
        ownership_generation=ownership.ownership_generation,
        account_id=account.id,
        to=("+14155550101",),
        content="Approved reply",
        channel="SMS",
        idempotency_key="placement-human-send",
    )
    await _transition(placement_setup, "SUSPEND", 1)
    with pytest.raises(PlacementFencedError):
        await human_stack.service.send_message(
            organization.id, claim.assignment.id, request, actor_user_id=actor.user_id
        )
    assert human_stack.messaging.transport.requests == []
    async with human_stack.database.tenant_transaction(organization.id) as tenant:
        assert (
            await tenant.session.execute(text("SELECT count(*) FROM human_action_authorizations"))
        ).scalar_one() == 0
    await _transition(placement_setup, "RESUME", 2)
    consumed = await human_stack.service.send_message(
        organization.id, claim.assignment.id, request, actor_user_id=actor.user_id
    )
    assert consumed.state.value == "CONSUMED"
    assert (
        await human_stack.service.get_work_item(organization.id, claim.work_item.id)
    ).first_response_at is not None
    assert len(human_stack.messaging.transport.requests) == 1


async def test_placed_campaign_scheduled_bridge_replays_exact_release(
    placement_setup: Any, campaign_stack: Any, monkeypatch: Any
) -> None:
    from tests.integration.test_campaign_scheduled_release_bridge import (
        _bind,
        _dispatch_scheduled_release,
    )
    from tests.integration.test_campaign_service import campaign_principal

    organization, actor, service, cell_id = placement_setup
    monkeypatch.setattr(campaign_stack.database, "_worker_cell_id", cell_id)
    await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    _, run, occurrence = await _dispatch_scheduled_release(campaign_stack, organization.id)
    first, second = await asyncio.gather(
        _bind(campaign_stack, organization.id, run, occurrence),
        _bind(campaign_stack, organization.id, run, occurrence),
    )
    assert (
        first.release_workflow_run_id
        == second.release_workflow_run_id
        == occurrence.workflow_run_id
    )
    assert (
        first.release_schedule_occurrence_id
        == second.release_schedule_occurrence_id
        == occurrence.id
    )
    await campaign_stack.workflows.execute_next(
        campaign_principal(organization.id), occurrence.workflow_run_id
    )
    active = await campaign_stack.service.confirm_release(organization.id, run.id)
    assert active.id == run.id and active.state.value == "RUNNING"
    async with campaign_stack.database.tenant_transaction(organization.id) as tenant:
        attempts = (
            (
                await tenant.session.execute(
                    text("SELECT campaign_run_id FROM campaign_recipient_attempts")
                )
            )
            .scalars()
            .all()
        )
    assert attempts == [run.id]


async def test_workflow_result_after_suspend_does_not_unlock_new_steps(
    placement_setup: Any, workflow_stack: Any, monkeypatch: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    monkeypatch.setattr(workflow_stack.database, "_worker_cell_id", cell_id)
    await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    _, version = await _published(
        workflow_stack, organization.id, (_noop("first"), _noop("next", "first"))
    )
    run = await workflow_stack.service.start_run(
        organization.id,
        StartWorkflowRunRequest(workflow_version_id=version.id, idempotency_key="suspended-result"),
    )
    original = workflow_stack.service._execute
    invocations: list[Any] = []

    async def suspend_after_execution(principal: Any, claim: Any) -> Any:
        invocations.append(claim.step.id)
        result = await original(principal, claim)
        await _transition(placement_setup, "SUSPEND", 1)
        return result

    monkeypatch.setattr(workflow_stack.service, "_execute", suspend_after_execution)
    with pytest.raises(PlacementFencedError):
        await workflow_stack.service.execute_next(actor, run.id)
    steps = await workflow_stack.service.list_steps(organization.id, run.id)
    states = {step.step_key: step.state.value for step in steps}
    assert states == {"first": "RUNNING", "next": "PENDING"}
    await _transition(placement_setup, "RESUME", 2)
    assert await workflow_stack.service.claim_next(organization.id, run.id) is None
    assert len(invocations) == 1
