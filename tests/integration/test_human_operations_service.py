"""P17 Human Operations through real PostgreSQL, P04, P09 and P13 boundaries."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select, text

from nexus_ai.agents.entities import AGENT_SESSION_CONTRACT_VERSION, StartAgentSessionRequest
from nexus_ai.domain.auth.entities import Principal
from nexus_ai.domain.customers.entities import CreateConversationRequest, CreateCustomerRequest
from nexus_ai.domain.humans.models import HumanHandoffRecord
from nexus_ai.humans.entities import (
    AssignmentActionRequest,
    ClaimWorkRequest,
    CreateQueueRequest,
    HumanChannel,
    HumanSendRequest,
    OwnershipMode,
    PresenceState,
    QueueHumanWorkRequest,
    ReturnToAiRequest,
    SetPresenceRequest,
    SupervisorActionRequest,
    SupervisorTransferToAgentRequest,
    TransferToAgentRequest,
    WorkItemState,
)
from nexus_ai.humans.errors import (
    HumanBoundaryUnavailableError,
    HumanConflictError,
    HumanExecutionFencedError,
    HumanInvalidStateError,
)
from nexus_ai.messaging.entities import (
    CreateAccountRequest,
    MessageChannel,
    StoreAccountCredentialRequest,
)
from nexus_ai.messaging.errors import MessagingProviderError
from tests.integration.test_agent_service import _provision

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _conversation(stack: Any, organization_id: uuid.UUID) -> tuple[Any, Any]:
    customer, _ = await stack.messaging.customers.resolve_or_create(
        organization_id,
        CreateCustomerRequest(
            display_name="Human customer",
            identity_type="PHONE",
            identity_value=f"+1415{uuid.uuid4().int % 10_000_000:07d}",
            identity_source="human-test",
        ),
    )
    conversation, _ = await stack.messaging.conversations.open_or_resolve(
        organization_id,
        CreateConversationRequest(customer_id=customer.id, channel="sms"),
    )
    return customer, conversation


async def _ready_work(
    stack: Any, organization: Any, principal: Principal, *, capacity: int = 1
) -> tuple[Any, Any, Any]:
    customer, conversation = await _conversation(stack, organization.id)
    queue = await stack.service.create_queue(
        organization.id,
        CreateQueueRequest(
            queue_key=f"support-{uuid.uuid4().hex[:8]}",
            name="Support",
            supported_channels=(HumanChannel.SMS,),
            max_active_assignments=capacity,
        ),
    )
    await stack.service.set_presence(
        organization.id,
        principal.user_id,
        SetPresenceRequest(state=PresenceState.AVAILABLE, capacity=capacity),
        actor_user_id=principal.user_id,
    )
    work = await stack.service.request_ai_handoff(
        organization.id,
        QueueHumanWorkRequest(
            conversation_id=conversation.id,
            customer_id=customer.id,
            channel=HumanChannel.SMS,
            queue_id=queue.id,
            handoff_reason="CUSTOMER_REQUESTED_HUMAN",
            source_type="AI_AGENT",
            source_id=uuid.uuid7(),
            idempotency_key=f"human:handoff:{uuid.uuid4().hex}",
        ),
    )
    return queue, work, conversation


async def _claimed(stack: Any, organization: Any, principal: Principal) -> tuple[Any, Any]:
    queue, _, conversation = await _ready_work(stack, organization, principal)
    claim = await stack.service.claim_work(
        organization.id,
        principal.user_id,
        ClaimWorkRequest(queue_id=queue.id, expected_queue_revision=queue.revision),
    )
    return claim, conversation


async def test_ai_to_human_claim_accept_and_complete_are_durable(
    human_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    claim, conversation = await _claimed(human_stack, organization, principal)
    accepted = await human_stack.service.accept_assignment(
        organization.id,
        claim.assignment.id,
        AssignmentActionRequest(
            claim_token=claim.claim_token,
            lease_version=claim.assignment.lease_version,
            ownership_generation=claim.ownership_generation,
        ),
        actor_user_id=principal.user_id,
    )
    assert accepted.state is WorkItemState.ACTIVE
    ownership = await human_stack.service.get_ownership(organization.id, conversation.id)
    assert ownership.mode is OwnershipMode.HUMAN
    completed = await human_stack.service.complete_assignment(
        organization.id,
        claim.assignment.id,
        AssignmentActionRequest(
            claim_token=claim.claim_token,
            lease_version=claim.assignment.lease_version,
            ownership_generation=ownership.ownership_generation,
            reason_code="CUSTOMER_RESOLVED",
        ),
        actor_user_id=principal.user_id,
    )
    assert completed.state is WorkItemState.COMPLETED


async def test_duplicate_accept_and_completion_are_idempotent(
    human_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    claim, conversation = await _claimed(human_stack, organization, principal)
    accept_request = AssignmentActionRequest(
        claim_token=claim.claim_token,
        lease_version=claim.assignment.lease_version,
        ownership_generation=claim.ownership_generation,
    )
    first_accept = await human_stack.service.accept_assignment(
        organization.id, claim.assignment.id, accept_request, actor_user_id=principal.user_id
    )
    second_accept = await human_stack.service.accept_assignment(
        organization.id, claim.assignment.id, accept_request, actor_user_id=principal.user_id
    )
    assert first_accept.state is WorkItemState.ACTIVE
    assert second_accept.id == first_accept.id
    ownership = await human_stack.service.get_ownership(organization.id, conversation.id)
    complete_request = AssignmentActionRequest(
        claim_token=claim.claim_token,
        lease_version=claim.assignment.lease_version,
        ownership_generation=ownership.ownership_generation,
        reason_code="CUSTOMER_RESOLVED",
    )
    first_complete = await human_stack.service.complete_assignment(
        organization.id, claim.assignment.id, complete_request, actor_user_id=principal.user_id
    )
    second_complete = await human_stack.service.complete_assignment(
        organization.id, claim.assignment.id, complete_request, actor_user_id=principal.user_id
    )
    assert first_complete.state is WorkItemState.COMPLETED
    assert second_complete.id == first_complete.id


async def test_stale_claim_token_is_fenced(
    human_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    claim, _ = await _claimed(human_stack, organization, principal)
    with pytest.raises(HumanExecutionFencedError):
        await human_stack.service.accept_assignment(
            organization.id,
            claim.assignment.id,
            AssignmentActionRequest(
                claim_token="wrong-token-" * 3,
                lease_version=claim.assignment.lease_version,
                ownership_generation=claim.ownership_generation,
            ),
            actor_user_id=principal.user_id,
        )


async def test_offline_agent_cannot_claim(
    human_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    queue, _, _ = await _ready_work(human_stack, organization, principal)
    current = (await human_stack.service.list_presence(organization.id, limit=10, offset=0))[0]
    await human_stack.service.set_presence(
        organization.id,
        principal.user_id,
        SetPresenceRequest(
            state=PresenceState.OFFLINE, capacity=0, expected_version=current.version
        ),
        actor_user_id=principal.user_id,
    )
    with pytest.raises(HumanInvalidStateError):
        await human_stack.service.claim_work(
            organization.id,
            principal.user_id,
            ClaimWorkRequest(queue_id=queue.id, expected_queue_revision=queue.revision),
        )


async def test_duplicate_ai_handoff_reuses_one_work_item(
    human_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    customer, conversation = await _conversation(human_stack, organization.id)
    queue = await human_stack.service.create_queue(
        organization.id,
        CreateQueueRequest(
            queue_key="duplicate", name="Duplicate", supported_channels=(HumanChannel.SMS,)
        ),
    )
    payload = QueueHumanWorkRequest(
        conversation_id=conversation.id,
        customer_id=customer.id,
        channel=HumanChannel.SMS,
        queue_id=queue.id,
        handoff_reason="AI_ESCALATION",
        source_type="AI_AGENT",
        idempotency_key="human:handoff:duplicate",
    )
    first = await human_stack.service.request_ai_handoff(
        organization.id, payload, actor_user_id=principal.user_id
    )
    second = await human_stack.service.request_ai_handoff(
        organization.id, payload, actor_user_id=principal.user_id
    )
    assert first.id == second.id


async def test_human_send_uses_p09_once_and_persists_authorization(
    human_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    claim, conversation = await _claimed(human_stack, organization, principal)
    await human_stack.service.accept_assignment(
        organization.id,
        claim.assignment.id,
        AssignmentActionRequest(
            claim_token=claim.claim_token,
            lease_version=claim.assignment.lease_version,
            ownership_generation=claim.ownership_generation,
        ),
        actor_user_id=principal.user_id,
    )
    ownership = await human_stack.service.get_ownership(organization.id, conversation.id)
    account = await human_stack.messaging.service.create_account(
        organization.id,
        CreateAccountRequest(
            channel=MessageChannel.SMS,
            provider="generic_http",
            slug="human-send",
            external_account_id="human-send",
            sender_identity="+14155550100",
        ),
    )
    await human_stack.messaging.service.store_account_credential(
        organization.id,
        account.id,
        StoreAccountCredentialRequest(fields={"api_token": "test-provider-token"}),
    )
    request = HumanSendRequest(
        claim_token=claim.claim_token,
        lease_version=claim.assignment.lease_version,
        ownership_generation=ownership.ownership_generation,
        account_id=account.id,
        to=("+14155550101",),
        content="A human-approved response",
        channel=HumanChannel.SMS,
        idempotency_key="human:message:once",
    )
    first = await human_stack.service.send_message(
        organization.id, claim.assignment.id, request, actor_user_id=principal.user_id
    )
    after_first = await human_stack.service.get_work_item(organization.id, claim.work_item.id)
    assert after_first.first_response_at is not None
    second = await human_stack.service.send_message(
        organization.id, claim.assignment.id, request, actor_user_id=principal.user_id
    )
    after_replay = await human_stack.service.get_work_item(organization.id, claim.work_item.id)
    assert after_replay.first_response_at == after_first.first_response_at
    later_request = request.model_copy(
        update={
            "content": "A second human-approved response",
            "idempotency_key": "human:message:second",
        }
    )
    human_stack.messaging.transport.set_response(
        200, {"messages": [{"id": "prov-msg-2"}], "message_id": "prov-msg-2"}
    )
    await human_stack.service.send_message(
        organization.id, claim.assignment.id, later_request, actor_user_id=principal.user_id
    )
    after_second = await human_stack.service.get_work_item(organization.id, claim.work_item.id)
    assert after_second.first_response_at == after_first.first_response_at
    assert first.message_id == second.message_id
    assert len(human_stack.messaging.transport.requests) == 2


async def test_failed_p09_send_does_not_set_first_response(
    human_stack: Any,
    make_organization: Any,
    make_tool_principal: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    claim, conversation = await _claimed(human_stack, organization, principal)
    await human_stack.service.accept_assignment(
        organization.id,
        claim.assignment.id,
        AssignmentActionRequest(
            claim_token=claim.claim_token,
            lease_version=claim.assignment.lease_version,
            ownership_generation=claim.ownership_generation,
        ),
        actor_user_id=principal.user_id,
    )
    ownership = await human_stack.service.get_ownership(organization.id, conversation.id)

    async def _failed(*args: Any, **kwargs: Any) -> Any:
        raise MessagingProviderError("known provider rejection")

    monkeypatch.setattr(human_stack.messaging.service, "send", _failed)
    with pytest.raises(MessagingProviderError):
        await human_stack.service.send_message(
            organization.id,
            claim.assignment.id,
            HumanSendRequest(
                claim_token=claim.claim_token,
                lease_version=claim.assignment.lease_version,
                ownership_generation=ownership.ownership_generation,
                account_id=uuid.uuid7(),
                to=("+14155550101",),
                content="Known failure",
                channel=HumanChannel.SMS,
                idempotency_key="human:message:known-failure",
            ),
            actor_user_id=principal.user_id,
        )
    persisted = await human_stack.service.get_work_item(organization.id, claim.work_item.id)
    assert persisted.first_response_at is None


async def test_ambiguous_p09_delivery_is_not_automatically_retried(
    human_stack: Any,
    make_organization: Any,
    make_tool_principal: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    claim, conversation = await _claimed(human_stack, organization, principal)
    await human_stack.service.accept_assignment(
        organization.id,
        claim.assignment.id,
        AssignmentActionRequest(
            claim_token=claim.claim_token,
            lease_version=claim.assignment.lease_version,
            ownership_generation=claim.ownership_generation,
        ),
        actor_user_id=principal.user_id,
    )
    ownership = await human_stack.service.get_ownership(organization.id, conversation.id)
    calls = 0

    async def _ambiguous(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise RuntimeError("provider result lost")

    monkeypatch.setattr(human_stack.messaging.service, "send", _ambiguous)
    request = HumanSendRequest(
        claim_token=claim.claim_token,
        lease_version=claim.assignment.lease_version,
        ownership_generation=ownership.ownership_generation,
        account_id=uuid.uuid7(),
        to=("+14155550101",),
        content="Ambiguous response",
        channel=HumanChannel.SMS,
        idempotency_key="human:message:ambiguous",
    )
    for _ in range(2):
        with pytest.raises(HumanBoundaryUnavailableError):
            await human_stack.service.send_message(
                organization.id, claim.assignment.id, request, actor_user_id=principal.user_id
            )
    assert calls == 1
    persisted = await human_stack.service.get_work_item(organization.id, claim.work_item.id)
    assert persisted.first_response_at is None


async def test_p09_accepted_local_write_loss_reuses_same_logical_message(
    human_stack: Any,
    make_organization: Any,
    make_tool_principal: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    claim, conversation = await _claimed(human_stack, organization, principal)
    await human_stack.service.accept_assignment(
        organization.id,
        claim.assignment.id,
        AssignmentActionRequest(
            claim_token=claim.claim_token,
            lease_version=claim.assignment.lease_version,
            ownership_generation=claim.ownership_generation,
        ),
        actor_user_id=principal.user_id,
    )
    ownership = await human_stack.service.get_ownership(organization.id, conversation.id)
    account = await human_stack.messaging.service.create_account(
        organization.id,
        CreateAccountRequest(
            channel=MessageChannel.SMS,
            provider="generic_http",
            slug="human-write-loss",
            external_account_id="human-write-loss",
            sender_identity="+14155550100",
        ),
    )
    await human_stack.messaging.service.store_account_credential(
        organization.id,
        account.id,
        StoreAccountCredentialRequest(fields={"api_token": "test-provider-token"}),
    )
    request = HumanSendRequest(
        claim_token=claim.claim_token,
        lease_version=claim.assignment.lease_version,
        ownership_generation=ownership.ownership_generation,
        account_id=account.id,
        to=("+14155550101",),
        content="Persist this once",
        channel=HumanChannel.SMS,
        idempotency_key="human:message:write-loss",
    )
    original = human_stack.service._finalize_authorization
    finalize_calls = 0

    async def _lose_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal finalize_calls
        finalize_calls += 1
        if finalize_calls == 1:
            raise RuntimeError("terminal write lost")
        return await original(*args, **kwargs)

    monkeypatch.setattr(human_stack.service, "_finalize_authorization", _lose_once)
    with pytest.raises(RuntimeError, match="terminal write lost"):
        await human_stack.service.send_message(
            organization.id, claim.assignment.id, request, actor_user_id=principal.user_id
        )
    replay = await human_stack.service.send_message(
        organization.id, claim.assignment.id, request, actor_user_id=principal.user_id
    )
    assert replay.message_id is not None
    assert len(human_stack.messaging.transport.requests) == 1


async def test_human_to_ai_binds_exact_p13_session(
    human_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    agent = await _provision(human_stack.agents, organization.id)
    claim, conversation = await _claimed(human_stack, organization, principal)
    await human_stack.service.accept_assignment(
        organization.id,
        claim.assignment.id,
        AssignmentActionRequest(
            claim_token=claim.claim_token,
            lease_version=claim.assignment.lease_version,
            ownership_generation=claim.ownership_generation,
        ),
        actor_user_id=principal.user_id,
    )
    ownership = await human_stack.service.get_ownership(organization.id, conversation.id)
    returned = await human_stack.service.return_to_ai(
        organization.id,
        claim.assignment.id,
        ReturnToAiRequest(
            claim_token=claim.claim_token,
            lease_version=claim.assignment.lease_version,
            ownership_generation=ownership.ownership_generation,
            agent_id=agent.id,
            context="Continue assisting this customer.",
            idempotency_key="human:return:exact",
        ),
        principal=principal,
    )
    assert returned.mode is OwnershipMode.AI
    assert returned.ai_session_id is not None
    async with human_stack.database.tenant_transaction(organization.id) as tenant:
        handoff = (
            await tenant.session.execute(
                select(HumanHandoffRecord).where(
                    HumanHandoffRecord.organization_id == organization.id,
                    HumanHandoffRecord.idempotency_key == "human:return:exact",
                )
            )
        ).scalar_one()
        assert handoff.p13_contract_version == AGENT_SESSION_CONTRACT_VERSION
    replay = await human_stack.service.return_to_ai(
        organization.id,
        claim.assignment.id,
        ReturnToAiRequest(
            claim_token=claim.claim_token,
            lease_version=claim.assignment.lease_version,
            ownership_generation=ownership.ownership_generation,
            agent_id=agent.id,
            context="Continue assisting this customer.",
            idempotency_key="human:return:exact",
        ),
        principal=principal,
    )
    assert replay.ai_session_id == returned.ai_session_id
    with pytest.raises(HumanConflictError):
        await human_stack.service.return_to_ai(
            organization.id,
            claim.assignment.id,
            ReturnToAiRequest(
                claim_token=claim.claim_token,
                lease_version=claim.assignment.lease_version,
                ownership_generation=ownership.ownership_generation,
                agent_id=uuid.uuid7(),
                context="Different semantics.",
                idempotency_key="human:return:exact",
            ),
            principal=principal,
        )


async def test_human_to_ai_contract_version_mismatch_fails_closed(
    human_stack: Any,
    make_organization: Any,
    make_tool_principal: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    agent = await _provision(human_stack.agents, organization.id)
    claim, conversation = await _claimed(human_stack, organization, principal)
    await human_stack.service.accept_assignment(
        organization.id,
        claim.assignment.id,
        AssignmentActionRequest(
            claim_token=claim.claim_token,
            lease_version=claim.assignment.lease_version,
            ownership_generation=claim.ownership_generation,
        ),
        actor_user_id=principal.user_id,
    )
    ownership = await human_stack.service.get_ownership(organization.id, conversation.id)
    original = human_stack.agents.service.start_session

    async def _wrong_contract(*args: Any, **kwargs: Any) -> Any:
        session = await original(*args, **kwargs)
        return SimpleNamespace(
            **session.model_dump(),
            CONTRACT_VERSION="NXS-P13.agent-session.v999",
        )

    monkeypatch.setattr(human_stack.agents.service, "start_session", _wrong_contract)
    with pytest.raises(HumanExecutionFencedError, match="contract version"):
        await human_stack.service.return_to_ai(
            organization.id,
            claim.assignment.id,
            ReturnToAiRequest(
                claim_token=claim.claim_token,
                lease_version=claim.assignment.lease_version,
                ownership_generation=ownership.ownership_generation,
                agent_id=agent.id,
                context="Continue under the expected contract.",
                idempotency_key="human:return:contract-mismatch",
            ),
            principal=principal,
        )
    persisted = await human_stack.service.get_ownership(organization.id, conversation.id)
    assert persisted.mode is OwnershipMode.UNASSIGNED
    assert persisted.ai_session_id is None


async def test_human_to_ai_conflicting_execution_binding_fails_closed(
    human_stack: Any,
    make_organization: Any,
    make_tool_principal: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    agent = await _provision(human_stack.agents, organization.id)
    claim, conversation = await _claimed(human_stack, organization, principal)
    await human_stack.service.accept_assignment(
        organization.id,
        claim.assignment.id,
        AssignmentActionRequest(
            claim_token=claim.claim_token,
            lease_version=claim.assignment.lease_version,
            ownership_generation=claim.ownership_generation,
        ),
        actor_user_id=principal.user_id,
    )
    ownership = await human_stack.service.get_ownership(organization.id, conversation.id)
    original = human_stack.agents.service.start_session

    async def _conflicting_binding(
        organization_id: uuid.UUID, boundary_principal: Principal, request: StartAgentSessionRequest
    ) -> Any:
        expected = await original(organization_id, boundary_principal, request)
        conflict = await original(
            organization_id,
            boundary_principal,
            request.model_copy(update={"idempotency_key": f"{request.idempotency_key}:other"}),
        )
        async with human_stack.database.tenant_transaction(organization_id) as tenant:
            handoff = (
                await tenant.session.execute(
                    select(HumanHandoffRecord)
                    .where(
                        HumanHandoffRecord.organization_id == organization_id,
                        HumanHandoffRecord.idempotency_key == "human:return:execution-conflict",
                    )
                    .with_for_update()
                )
            ).scalar_one()
            handoff.p13_session_id = conflict.id
        return expected

    monkeypatch.setattr(human_stack.agents.service, "start_session", _conflicting_binding)
    with pytest.raises(HumanConflictError, match="different P13 execution"):
        await human_stack.service.return_to_ai(
            organization.id,
            claim.assignment.id,
            ReturnToAiRequest(
                claim_token=claim.claim_token,
                lease_version=claim.assignment.lease_version,
                ownership_generation=ownership.ownership_generation,
                agent_id=agent.id,
                context="Bind exactly one execution.",
                idempotency_key="human:return:execution-conflict",
            ),
            principal=principal,
        )
    persisted = await human_stack.service.get_ownership(organization.id, conversation.id)
    assert persisted.mode is OwnershipMode.UNASSIGNED
    assert persisted.ai_session_id is None


async def test_known_p13_rejection_requeues_without_restoring_human_authority(
    human_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    claim, conversation = await _claimed(human_stack, organization, principal)
    await human_stack.service.accept_assignment(
        organization.id,
        claim.assignment.id,
        AssignmentActionRequest(
            claim_token=claim.claim_token,
            lease_version=claim.assignment.lease_version,
            ownership_generation=claim.ownership_generation,
        ),
        actor_user_id=principal.user_id,
    )
    ownership = await human_stack.service.get_ownership(organization.id, conversation.id)
    with pytest.raises(HumanInvalidStateError):
        await human_stack.service.return_to_ai(
            organization.id,
            claim.assignment.id,
            ReturnToAiRequest(
                claim_token=claim.claim_token,
                lease_version=claim.assignment.lease_version,
                ownership_generation=ownership.ownership_generation,
                agent_id=uuid.uuid7(),
                context="Continue.",
                idempotency_key="human:return:rejected",
            ),
            principal=principal,
        )
    persisted = await human_stack.service.get_work_item(organization.id, claim.work_item.id)
    ownership = await human_stack.service.get_ownership(organization.id, conversation.id)
    assert persisted.state is WorkItemState.QUEUED
    assert ownership.mode is OwnershipMode.UNASSIGNED


async def test_ambiguous_p13_acceptance_stays_pending_for_p25(
    human_stack: Any,
    make_organization: Any,
    make_tool_principal: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    claim, conversation = await _claimed(human_stack, organization, principal)
    await human_stack.service.accept_assignment(
        organization.id,
        claim.assignment.id,
        AssignmentActionRequest(
            claim_token=claim.claim_token,
            lease_version=claim.assignment.lease_version,
            ownership_generation=claim.ownership_generation,
        ),
        actor_user_id=principal.user_id,
    )
    ownership = await human_stack.service.get_ownership(organization.id, conversation.id)

    async def _ambiguous(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("response lost after possible accept")

    calls = 0

    async def _counted_ambiguous(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return await _ambiguous(*args, **kwargs)

    monkeypatch.setattr(human_stack.agents.service, "start_session", _counted_ambiguous)
    request = ReturnToAiRequest(
        claim_token=claim.claim_token,
        lease_version=claim.assignment.lease_version,
        ownership_generation=ownership.ownership_generation,
        agent_id=uuid.uuid7(),
        context="Continue.",
        idempotency_key="human:return:ambiguous",
    )
    with pytest.raises(HumanBoundaryUnavailableError):
        await human_stack.service.return_to_ai(
            organization.id,
            claim.assignment.id,
            request,
            principal=principal,
        )
    with pytest.raises(HumanBoundaryUnavailableError):
        await human_stack.service.return_to_ai(
            organization.id, claim.assignment.id, request, principal=principal
        )
    assert calls == 1
    persisted = await human_stack.service.get_work_item(organization.id, claim.work_item.id)
    ownership = await human_stack.service.get_ownership(organization.id, conversation.id)
    assert persisted.state is WorkItemState.AI_RETURN_PENDING
    assert ownership.mode is OwnershipMode.UNASSIGNED


async def test_agent_transfer_issues_new_token_and_fences_old_owner(
    human_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    first = await make_tool_principal(organization)
    second = await make_tool_principal(organization)
    claim, conversation = await _claimed(human_stack, organization, first)
    await human_stack.service.accept_assignment(
        organization.id,
        claim.assignment.id,
        AssignmentActionRequest(
            claim_token=claim.claim_token,
            lease_version=claim.assignment.lease_version,
            ownership_generation=claim.ownership_generation,
        ),
        actor_user_id=first.user_id,
    )
    ownership = await human_stack.service.get_ownership(organization.id, conversation.id)
    await human_stack.service.set_presence(
        organization.id,
        second.user_id,
        SetPresenceRequest(state=PresenceState.AVAILABLE, capacity=1),
        actor_user_id=second.user_id,
    )
    transferred = await human_stack.service.transfer_to_agent(
        organization.id,
        claim.assignment.id,
        TransferToAgentRequest(
            claim_token=claim.claim_token,
            lease_version=claim.assignment.lease_version,
            ownership_generation=ownership.ownership_generation,
            target_agent_user_id=second.user_id,
            reason_code="SPECIALIST_TRANSFER",
        ),
        actor_user_id=first.user_id,
    )
    assert transferred.assignment.owner_agent_id == second.user_id
    assert transferred.claim_token != claim.claim_token
    with pytest.raises(HumanExecutionFencedError):
        await human_stack.service.complete_assignment(
            organization.id,
            claim.assignment.id,
            AssignmentActionRequest(
                claim_token=claim.claim_token,
                lease_version=claim.assignment.lease_version,
                ownership_generation=ownership.ownership_generation,
            ),
            actor_user_id=first.user_id,
        )


async def test_supervisor_agent_transfer_issues_fresh_authority(
    human_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    first = await make_tool_principal(organization)
    second = await make_tool_principal(organization)
    claim, _ = await _claimed(human_stack, organization, first)
    await human_stack.service.set_presence(
        organization.id,
        second.user_id,
        SetPresenceRequest(state=PresenceState.AVAILABLE, capacity=1),
        actor_user_id=second.user_id,
    )
    transferred = await human_stack.service.supervisor_transfer_to_agent(
        organization.id,
        claim.assignment.id,
        SupervisorTransferToAgentRequest(
            target_agent_user_id=second.user_id,
            reason_code="SUPERVISOR_TRANSFER",
        ),
        actor_user_id=first.user_id,
    )
    assert transferred.assignment.owner_agent_id == second.user_id
    assert transferred.assignment.lease_version == claim.assignment.lease_version + 1
    assert transferred.claim_token != claim.claim_token
    with pytest.raises(HumanExecutionFencedError):
        await human_stack.service.accept_assignment(
            organization.id,
            claim.assignment.id,
            AssignmentActionRequest(
                claim_token=claim.claim_token,
                lease_version=claim.assignment.lease_version,
                ownership_generation=claim.ownership_generation,
            ),
            actor_user_id=first.user_id,
        )


async def test_handoff_idempotency_key_rejects_different_semantics(
    human_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    queue, work, _ = await _ready_work(human_stack, organization, principal)
    customer, conversation = await _conversation(human_stack, organization.id)
    with pytest.raises(HumanConflictError):
        await human_stack.service.request_ai_handoff(
            organization.id,
            QueueHumanWorkRequest(
                conversation_id=conversation.id,
                customer_id=customer.id,
                channel=HumanChannel.SMS,
                queue_id=queue.id,
                handoff_reason="AI_ESCALATION",
                source_type="AI_AGENT",
                idempotency_key=work.idempotency_key,
            ),
            actor_user_id=principal.user_id,
        )


async def test_supervisor_cancel_is_idempotent_and_terminal(
    human_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    _, work, _ = await _ready_work(human_stack, organization, principal)
    request = SupervisorActionRequest(reason_code="SUPERVISOR_CANCELLED")
    first = await human_stack.service.cancel_work(
        organization.id, work.id, request, actor_user_id=principal.user_id
    )
    second = await human_stack.service.cancel_work(
        organization.id, work.id, request, actor_user_id=principal.user_id
    )
    assert first.state is WorkItemState.CANCELLED
    assert second.state is WorkItemState.CANCELLED


async def test_cross_tenant_work_item_is_invisible(
    human_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    first_org = await make_organization()
    second_org = await make_organization()
    principal = await make_tool_principal(first_org)
    _, work, _ = await _ready_work(human_stack, first_org, principal)
    with pytest.raises(Exception) as caught:
        await human_stack.service.get_work_item(second_org.id, work.id)
    assert getattr(caught.value, "code", None) == "NXS_HUMAN_NOT_FOUND"


async def test_business_state_and_outbox_commit_atomically(
    human_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    _, work, _ = await _ready_work(human_stack, organization, principal)
    async with human_stack.database.tenant_transaction(organization.id) as tenant:
        count = (
            await tenant.session.execute(
                text(
                    "SELECT count(*) FROM event_outbox "
                    "WHERE envelope->>'aggregate_id'=:id "
                    "AND event_type='human.work.queued'"
                ),
                {"id": str(work.id)},
            )
        ).scalar_one()
    assert count == 1
