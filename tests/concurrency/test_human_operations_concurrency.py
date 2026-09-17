"""Real PostgreSQL multi-worker certification for P17 authority races."""

import asyncio
import uuid
from typing import Any

import pytest

from nexus_ai.humans.entities import (
    AssignmentActionRequest,
    ClaimWorkRequest,
    HumanChannel,
    HumanSendRequest,
    PresenceState,
    SetPresenceRequest,
    SupervisorActionRequest,
    TransferToAgentRequest,
    UpdateQueueRequest,
)
from nexus_ai.humans.errors import (
    HumanCapacityExceededError,
    HumanConflictError,
    HumanExecutionFencedError,
    HumanInvalidStateError,
    HumanNotFoundError,
)
from nexus_ai.messaging.entities import (
    CreateAccountRequest,
    MessageChannel,
    StoreAccountCredentialRequest,
)
from tests.integration.test_human_operations_service import _claimed, _ready_work

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_two_agents_cannot_claim_same_work_item(
    human_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    first = await make_tool_principal(organization)
    second = await make_tool_principal(organization)
    queue, _, _ = await _ready_work(human_stack, organization, first)
    await human_stack.service.set_presence(
        organization.id,
        second.user_id,
        SetPresenceRequest(state=PresenceState.AVAILABLE, capacity=1),
        actor_user_id=second.user_id,
    )
    results = await asyncio.gather(
        human_stack.service.claim_work(
            organization.id,
            first.user_id,
            ClaimWorkRequest(queue_id=queue.id, expected_queue_revision=queue.revision),
        ),
        human_stack.service.claim_work(
            organization.id,
            second.user_id,
            ClaimWorkRequest(queue_id=queue.id, expected_queue_revision=queue.revision),
        ),
        return_exceptions=True,
    )
    assert len([item for item in results if not isinstance(item, BaseException)]) == 1
    assert len([item for item in results if isinstance(item, HumanNotFoundError)]) == 1


async def test_same_agent_cannot_exceed_capacity_under_concurrent_claims(
    human_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    queue, _, _ = await _ready_work(human_stack, organization, principal, capacity=1)
    await _ready_work(human_stack, organization, principal, capacity=1)
    results = await asyncio.gather(
        human_stack.service.claim_work(
            organization.id,
            principal.user_id,
            ClaimWorkRequest(queue_id=queue.id, expected_queue_revision=queue.revision),
        ),
        human_stack.service.claim_work(
            organization.id,
            principal.user_id,
            ClaimWorkRequest(queue_id=queue.id, expected_queue_revision=queue.revision),
        ),
        return_exceptions=True,
    )
    assert len([item for item in results if not isinstance(item, BaseException)]) == 1
    assert any(isinstance(item, HumanCapacityExceededError) for item in results)


async def test_queue_disable_and_claim_are_first_commit_deterministic(
    human_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    queue, _, _ = await _ready_work(human_stack, organization, principal)
    results = await asyncio.gather(
        human_stack.service.claim_work(
            organization.id,
            principal.user_id,
            ClaimWorkRequest(queue_id=queue.id, expected_queue_revision=queue.revision),
        ),
        human_stack.service.update_queue(
            organization.id,
            queue.id,
            UpdateQueueRequest(expected_revision=queue.revision, enabled=False),
        ),
        return_exceptions=True,
    )
    claim, update = results
    assert not (isinstance(claim, BaseException) and isinstance(update, BaseException))
    if isinstance(claim, BaseException):
        assert isinstance(claim, (HumanConflictError, HumanInvalidStateError, HumanNotFoundError))
    else:
        assert claim.assignment.id is not None


async def test_queue_revision_fences_stale_claim(
    human_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    queue, _, _ = await _ready_work(human_stack, organization, principal)
    await human_stack.service.update_queue(
        organization.id,
        queue.id,
        UpdateQueueRequest(expected_revision=queue.revision, name="Updated"),
    )
    with pytest.raises(HumanConflictError):
        await human_stack.service.claim_work(
            organization.id,
            principal.user_id,
            ClaimWorkRequest(queue_id=queue.id, expected_queue_revision=queue.revision),
        )


async def _active_assignment(stack: Any, organization: Any, principal: Any) -> tuple[Any, Any]:
    claim, conversation = await _claimed(stack, organization, principal)
    await stack.service.accept_assignment(
        organization.id,
        claim.assignment.id,
        AssignmentActionRequest(
            claim_token=claim.claim_token,
            lease_version=claim.assignment.lease_version,
            ownership_generation=claim.ownership_generation,
        ),
        actor_user_id=principal.user_id,
    )
    ownership = await stack.service.get_ownership(organization.id, conversation.id)
    return claim, ownership


async def _message_request(stack: Any, organization: Any, claim: Any, ownership: Any) -> Any:
    account = await stack.messaging.service.create_account(
        organization.id,
        CreateAccountRequest(
            channel=MessageChannel.SMS,
            provider="generic_http",
            slug=f"race-{uuid.uuid4().hex[:8]}",
            external_account_id=f"race-{uuid.uuid4().hex[:8]}",
            sender_identity="+14155550100",
        ),
    )
    await stack.messaging.service.store_account_credential(
        organization.id,
        account.id,
        StoreAccountCredentialRequest(fields={"api_token": "test-provider-token"}),
    )
    return HumanSendRequest(
        claim_token=claim.claim_token,
        lease_version=claim.assignment.lease_version,
        ownership_generation=ownership.ownership_generation,
        account_id=account.id,
        to=("+14155550101",),
        content="Race-fenced human response",
        channel=HumanChannel.SMS,
        idempotency_key=f"human:race:{uuid.uuid4().hex}",
    )


async def test_transfer_and_reply_serialize_on_human_authority(
    human_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    source = await make_tool_principal(organization)
    target = await make_tool_principal(organization)
    claim, ownership = await _active_assignment(human_stack, organization, source)
    await human_stack.service.set_presence(
        organization.id,
        target.user_id,
        SetPresenceRequest(state=PresenceState.AVAILABLE, capacity=1),
        actor_user_id=target.user_id,
    )
    message = await _message_request(human_stack, organization, claim, ownership)
    results = await asyncio.gather(
        human_stack.service.send_message(
            organization.id, claim.assignment.id, message, actor_user_id=source.user_id
        ),
        human_stack.service.transfer_to_agent(
            organization.id,
            claim.assignment.id,
            TransferToAgentRequest(
                claim_token=claim.claim_token,
                lease_version=claim.assignment.lease_version,
                ownership_generation=ownership.ownership_generation,
                target_agent_user_id=target.user_id,
                reason_code="RACE_TRANSFER",
            ),
            actor_user_id=source.user_id,
        ),
        return_exceptions=True,
    )
    assert not isinstance(results[1], BaseException)
    if isinstance(results[0], BaseException):
        assert isinstance(results[0], HumanExecutionFencedError)
    assert len(human_stack.messaging.transport.requests) <= 1


async def test_supervisor_release_and_reply_serialize_on_human_authority(
    human_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    agent = await make_tool_principal(organization)
    claim, ownership = await _active_assignment(human_stack, organization, agent)
    message = await _message_request(human_stack, organization, claim, ownership)
    results = await asyncio.gather(
        human_stack.service.send_message(
            organization.id, claim.assignment.id, message, actor_user_id=agent.user_id
        ),
        human_stack.service.supervisor_release(
            organization.id,
            claim.assignment.id,
            SupervisorActionRequest(reason_code="SUPERVISOR_RACE_RELEASE"),
            actor_user_id=agent.user_id,
        ),
        return_exceptions=True,
    )
    assert not isinstance(results[1], BaseException)
    assert len(human_stack.messaging.transport.requests) <= 1


async def test_two_transfers_cannot_oversubscribe_target_capacity(
    human_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    first = await make_tool_principal(organization)
    second = await make_tool_principal(organization)
    target = await make_tool_principal(organization)
    first_claim, first_ownership = await _active_assignment(human_stack, organization, first)
    second_claim, second_ownership = await _active_assignment(human_stack, organization, second)
    await human_stack.service.set_presence(
        organization.id,
        target.user_id,
        SetPresenceRequest(state=PresenceState.AVAILABLE, capacity=1),
        actor_user_id=target.user_id,
    )
    results = await asyncio.gather(
        human_stack.service.transfer_to_agent(
            organization.id,
            first_claim.assignment.id,
            TransferToAgentRequest(
                claim_token=first_claim.claim_token,
                lease_version=first_claim.assignment.lease_version,
                ownership_generation=first_ownership.ownership_generation,
                target_agent_user_id=target.user_id,
                reason_code="CAPACITY_RACE",
            ),
            actor_user_id=first.user_id,
        ),
        human_stack.service.transfer_to_agent(
            organization.id,
            second_claim.assignment.id,
            TransferToAgentRequest(
                claim_token=second_claim.claim_token,
                lease_version=second_claim.assignment.lease_version,
                ownership_generation=second_ownership.ownership_generation,
                target_agent_user_id=target.user_id,
                reason_code="CAPACITY_RACE",
            ),
            actor_user_id=second.user_id,
        ),
        return_exceptions=True,
    )
    assert len([result for result in results if not isinstance(result, BaseException)]) == 1
    assert (
        len([result for result in results if isinstance(result, HumanCapacityExceededError)]) == 1
    )
