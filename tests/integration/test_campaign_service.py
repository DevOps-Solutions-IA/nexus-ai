"""P16 Campaign integration through real PostgreSQL and P14/P09 boundaries."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from nexus_ai.campaigns.entities import (
    AudienceMemberInput,
    CampaignDraft,
    ContactPreferenceRequest,
    CreateCampaignRequest,
    QuietHoursPolicy,
    ScheduleCampaignRequest,
    SuppressionRequest,
    ThrottlePolicy,
)
from nexus_ai.campaigns.errors import (
    CampaignExecutionFencedError,
    CampaignInvalidStateError,
    CampaignNotFoundError,
)
from nexus_ai.campaigns.state_machine import CampaignState, RecipientState, SendPermitState
from nexus_ai.domain.auth.entities import Principal
from nexus_ai.domain.campaigns.models import (
    CampaignAudienceSnapshotRecord,
    CampaignRecipientAttemptRecord,
    CampaignRecipientRecord,
    CampaignRevisionRecord,
    CampaignSendPermitRecord,
)
from nexus_ai.domain.campaigns.repository import CampaignRepository
from nexus_ai.domain.customers.entities import CreateConversationRequest, CreateCustomerRequest
from nexus_ai.messaging.entities import (
    CreateAccountRequest,
    MessageChannel,
    MessageContent,
    StoreAccountCredentialRequest,
)
from nexus_ai.workflows.entities import (
    CreateWorkflowRequest,
    NoopStepConfig,
    WorkflowStepSpec,
    WorkflowStepType,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


def campaign_principal(organization_id: uuid.UUID) -> Principal:
    now = dt.datetime.now(dt.UTC)
    return Principal(
        user_id=uuid.uuid7(),
        session_id=uuid.uuid7(),
        organization_id=organization_id,
        token_id=uuid.uuid7(),
        issued_at=now,
        expires_at=now + dt.timedelta(hours=1),
    )


async def campaign_workflow(stack: Any, organization_id: uuid.UUID, suffix: str) -> Any:
    definition = await stack.workflows.create_definition(
        organization_id,
        CreateWorkflowRequest(
            workflow_key=f"campaign.{suffix}.{uuid.uuid4().hex[:8]}",
            name=f"Campaign {suffix}",
            steps=(
                WorkflowStepSpec(
                    key="done", step_type=WorkflowStepType.NOOP, config=NoopStepConfig()
                ),
            ),
        ),
    )
    return await stack.workflows.publish(organization_id, definition.id)


async def campaign_recipient(
    stack: Any, organization_id: uuid.UUID, *, suffix: int = 142
) -> tuple[Any, Any, Any]:
    customer, _ = await stack.messaging.customers.resolve_or_create(
        organization_id,
        CreateCustomerRequest(
            display_name="Campaign recipient",
            identity_type="PHONE",
            identity_value=f"+1415555{suffix:04d}",
            identity_source="campaign-test",
        ),
    )
    identity = (await stack.messaging.customers.list_identities(organization_id, customer.id))[0]
    conversation, _ = await stack.messaging.conversations.open_or_resolve(
        organization_id,
        CreateConversationRequest(customer_id=customer.id, channel="sms"),
    )
    return customer, identity, conversation


async def campaign_account(stack: Any, organization_id: uuid.UUID) -> Any:
    account = await stack.messaging.service.create_account(
        organization_id,
        CreateAccountRequest(
            channel=MessageChannel.SMS,
            provider="generic_http",
            slug=f"campaign-{uuid.uuid4().hex[:8]}",
            external_account_id=f"campaign-{uuid.uuid4().hex[:8]}",
            sender_identity="+14155550100",
        ),
    )
    await stack.messaging.service.store_account_credential(
        organization_id,
        account.id,
        StoreAccountCredentialRequest(fields={"api_token": "test-provider-token"}),
    )
    return account


async def campaign_draft(
    stack: Any, organization_id: uuid.UUID, **changes: Any
) -> tuple[CampaignDraft, Any]:
    release = await campaign_workflow(stack, organization_id, "release")
    recipient_workflow = await campaign_workflow(stack, organization_id, "recipient")
    customer, identity, conversation = await campaign_recipient(stack, organization_id)
    account = await campaign_account(stack, organization_id)
    values = {
        "release_workflow_version_id": release.id,
        "recipient_workflow_version_id": recipient_workflow.id,
        "account_id": account.id,
        "channel": MessageChannel.SMS,
        "content": MessageContent(text="A governed campaign message"),
        "audience": (
            AudienceMemberInput(
                customer_id=customer.id,
                identity_id=identity.id,
                conversation_id=conversation.id,
            ),
        ),
        "quiet_hours": QuietHoursPolicy(enabled=False),
        "throttle": ThrottlePolicy(messages_per_minute=10),
    }
    values.update(changes)
    return CampaignDraft(**values), (customer, identity, conversation)


async def prepared_campaign(stack: Any, organization_id: uuid.UUID) -> tuple[Any, Any]:
    draft, recipient = await campaign_draft(stack, organization_id)
    customer, identity, _ = recipient
    await stack.service.set_contact_preference(
        organization_id,
        ContactPreferenceRequest(
            customer_id=customer.id,
            identity_id=identity.id,
            channel=draft.channel,
            consent_granted=True,
            evidence_ref="consent:test",
        ),
    )
    campaign = await stack.service.create_campaign(
        organization_id,
        CreateCampaignRequest(
            campaign_key=f"campaign.{uuid.uuid4().hex[:8]}", name="Campaign", draft=draft
        ),
    )
    return await stack.service.prepare_campaign(organization_id, campaign.id), recipient


async def running_campaign(stack: Any, organization_id: uuid.UUID) -> tuple[Any, Any, Any]:
    campaign, recipient = await prepared_campaign(stack, organization_id)
    run = await stack.service.start_campaign(organization_id, campaign.id)
    assert run.release_workflow_run_id is not None
    await stack.workflows.execute_next(
        campaign_principal(organization_id), run.release_workflow_run_id
    )
    run = await stack.service.confirm_release(organization_id, run.id)
    return campaign, run, recipient


async def ready_claim(stack: Any, organization_id: uuid.UUID, run: Any) -> Any:
    claim = await stack.service.claim_recipient(organization_id, run.id, uuid.uuid7())
    assert claim is not None
    attempt = await stack.service.start_recipient_workflow(organization_id, claim)
    assert attempt.workflow_run_id is not None
    await stack.workflows.execute_next(campaign_principal(organization_id), attempt.workflow_run_id)
    await stack.service.confirm_recipient_workflow(organization_id, claim)
    return claim


async def test_full_flow_authorizes_then_sends_once_through_p09(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    campaign, run, (customer, identity, _) = await running_campaign(campaign_stack, organization.id)
    claim = await ready_claim(campaign_stack, organization.id, run)
    permit = await campaign_stack.service.authorize_send(organization.id, claim)
    assert permit.state is SendPermitState.AUTHORIZED

    consent_epoch = await campaign_stack.service.set_contact_preference(
        organization.id,
        ContactPreferenceRequest(
            customer_id=customer.id,
            identity_id=identity.id,
            channel="SMS",
            consent_granted=False,
            unsubscribed=True,
        ),
    )
    assert consent_epoch > permit.consent_epoch
    consumed = await campaign_stack.service.dispatch_send(organization.id, permit.id)
    replay = await campaign_stack.service.dispatch_send(organization.id, permit.id)
    assert consumed.state is SendPermitState.CONSUMED
    assert replay.message_id == consumed.message_id
    assert len(campaign_stack.messaging.transport.requests) == 1
    assert campaign.state is CampaignState.READY
    completed = await campaign_stack.service.get_campaign(organization.id, campaign.id)
    assert completed.state is CampaignState.COMPLETED


async def test_missing_consent_and_suppression_fail_closed(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    draft, (customer, identity, _) = await campaign_draft(campaign_stack, organization.id)
    campaign = await campaign_stack.service.create_campaign(
        organization.id,
        CreateCampaignRequest(campaign_key="campaign.no-consent", name="No consent", draft=draft),
    )
    await campaign_stack.service.prepare_campaign(organization.id, campaign.id)
    audience = await campaign_stack.service.audience(
        organization.id, campaign.id, limit=10, offset=0
    )
    assert audience[0].state is RecipientState.SUPPRESSED
    assert audience[0].eligibility_reason.value == "NO_CONSENT"
    epoch = await campaign_stack.service.add_suppression(
        organization.id,
        SuppressionRequest(
            channel="SMS",
            scope="IDENTITY",
            identity_id=identity.id,
            reason_code="CUSTOMER_OPT_OUT",
        ),
    )
    assert epoch >= 2
    assert customer.id == audience[0].customer_id


async def test_pause_cancel_and_terminal_absorption_block_new_claims(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    campaign, run, _ = await running_campaign(campaign_stack, organization.id)
    paused = await campaign_stack.service.pause_campaign(organization.id, campaign.id)
    assert paused.state is CampaignState.PAUSED
    assert (
        await campaign_stack.service.claim_recipient(organization.id, run.id, uuid.uuid7()) is None
    )
    resumed = await campaign_stack.service.resume_campaign(organization.id, campaign.id)
    assert resumed.state is CampaignState.RUNNING
    cancelled = await campaign_stack.service.cancel_campaign(organization.id, campaign.id)
    assert cancelled.state is CampaignState.CANCELLED
    assert (
        await campaign_stack.service.claim_recipient(organization.id, run.id, uuid.uuid7()) is None
    )
    with pytest.raises(CampaignInvalidStateError):
        await campaign_stack.service.resume_campaign(organization.id, campaign.id)


async def test_tenant_isolation_forced_rls_and_immutable_revision(
    campaign_stack: Any, make_organization: Any
) -> None:
    first, second = await make_organization(), await make_organization()
    campaign, _ = await prepared_campaign(campaign_stack, first.id)
    with pytest.raises(CampaignNotFoundError):
        await campaign_stack.service.get_campaign(second.id, campaign.id)
    async with campaign_stack.database.tenant_transaction(first.id) as tenant:
        revision = (await tenant.session.execute(select(CampaignRevisionRecord))).scalar_one()
        revision.content_hash = "0" * 64
        with pytest.raises(DBAPIError):
            await tenant.session.flush()
    async with campaign_stack.database.transaction() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname LIKE 'campaign%' AND relkind='r' ORDER BY relname"
                )
            )
        ).all()
    assert len(rows) == 13
    assert all(row[1] and row[2] for row in rows)


async def test_sealed_audience_and_recipient_identity_are_database_immutable(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    campaign, _ = await prepared_campaign(campaign_stack, organization.id)
    async with campaign_stack.database.tenant_transaction(organization.id) as tenant:
        snapshot = (
            await tenant.session.execute(select(CampaignAudienceSnapshotRecord))
        ).scalar_one()
        snapshot.source_digest = "0" * 64
        with pytest.raises(DBAPIError):
            await tenant.session.flush()
    async with campaign_stack.database.tenant_transaction(organization.id) as tenant:
        recipient = (await tenant.session.execute(select(CampaignRecipientRecord))).scalar_one()
        recipient.destination_fingerprint = "0" * 64
        with pytest.raises(DBAPIError):
            await tenant.session.flush()
    assert campaign.prepared_revision_id is not None


async def test_preparation_resumes_from_durable_open_snapshot(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    draft, (customer, identity, _) = await campaign_draft(campaign_stack, organization.id)
    await campaign_stack.service.set_contact_preference(
        organization.id,
        ContactPreferenceRequest(
            customer_id=customer.id,
            identity_id=identity.id,
            channel="SMS",
            consent_granted=True,
            evidence_ref="consent:resume",
        ),
    )
    campaign = await campaign_stack.service.create_campaign(
        organization.id,
        CreateCampaignRequest(campaign_key="campaign.resume", name="Resume", draft=draft),
    )
    async with campaign_stack.database.tenant_transaction(organization.id) as tenant:
        repo = CampaignRepository(tenant)
        row = await repo.campaign_row(campaign.id, for_update=True)
        assert row is not None
        _, open_snapshot = await repo.begin_preparation(row)
        assert open_snapshot.cursor == 0
    prepared = await campaign_stack.service.prepare_campaign(organization.id, campaign.id)
    assert prepared.state is CampaignState.READY
    audience = await campaign_stack.service.audience(
        organization.id, campaign.id, limit=10, offset=0
    )
    assert len(audience) == 1


async def test_scheduled_release_accepts_only_matching_p15_workflow_target(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    campaign, _ = await prepared_campaign(campaign_stack, organization.id)
    scheduled = await campaign_stack.service.schedule_campaign(
        organization.id,
        campaign.id,
        ScheduleCampaignRequest(
            schedule_type="ONE_TIME",
            timezone="UTC",
            start_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
        ),
    )
    assert scheduled.state is CampaignState.SCHEDULED
    runs = await campaign_stack.service.runs(organization.id, campaign.id, limit=10)
    assert len(runs) == 1
    assert runs[0].schedule_id is not None


async def test_stale_owner_is_fenced_before_permit(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    _, run, _ = await running_campaign(campaign_stack, organization.id)
    claim = await campaign_stack.service.claim_recipient(organization.id, run.id, uuid.uuid7())
    assert claim is not None
    attempt = await campaign_stack.service.start_recipient_workflow(organization.id, claim)
    assert attempt.workflow_run_id is not None
    await campaign_stack.workflows.execute_next(
        campaign_principal(organization.id), attempt.workflow_run_id
    )
    await campaign_stack.service.confirm_recipient_workflow(organization.id, claim)
    stale = claim.model_copy(update={"claim_token": uuid.uuid7()})
    with pytest.raises(CampaignExecutionFencedError):
        await campaign_stack.service.authorize_send(organization.id, stale)
    permit = await campaign_stack.service.authorize_send(organization.id, claim)
    async with campaign_stack.database.tenant_transaction(organization.id) as tenant:
        stored = (await tenant.session.execute(select(CampaignSendPermitRecord))).scalar_one()
        attempt_row = (
            await tenant.session.execute(select(CampaignRecipientAttemptRecord))
        ).scalar_one()
    assert stored.id == permit.id
    assert attempt_row.p14_idempotency_key != attempt_row.p09_idempotency_key
