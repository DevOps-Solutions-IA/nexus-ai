"""Consent, suppression, cancellation, quiet-hours and throttle send fences."""

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select

from nexus_ai.campaigns.entities import (
    ContactPreferenceRequest,
    CreateCampaignRequest,
    QuietHoursPolicy,
    SuppressionRequest,
    ThrottlePolicy,
)
from nexus_ai.campaigns.errors import CampaignExecutionFencedError, CampaignInvalidStateError
from nexus_ai.campaigns.state_machine import RecipientAttemptState, RecipientState
from nexus_ai.domain.campaigns.models import (
    CampaignRecipientAttemptRecord,
    CampaignRecipientRecord,
    CampaignSendPermitRecord,
    CampaignTransitionHistoryRecord,
)
from nexus_ai.domain.campaigns.repository import CampaignRepository
from tests.integration.test_campaign_service import (
    campaign_draft,
    campaign_principal,
    ready_claim,
    running_campaign,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _permit_count(stack: Any, organization_id: uuid.UUID) -> int:
    async with stack.database.tenant_transaction(organization_id) as tenant:
        return int(
            (
                await tenant.session.execute(select(func.count(CampaignSendPermitRecord.id)))
            ).scalar_one()
        )


async def test_unsubscribe_before_permit_blocks_authorization(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    _, run, (customer, identity, _) = await running_campaign(campaign_stack, organization.id)
    claim = await ready_claim(campaign_stack, organization.id, run)
    await campaign_stack.service.set_contact_preference(
        organization.id,
        ContactPreferenceRequest(
            customer_id=customer.id,
            identity_id=identity.id,
            channel="SMS",
            consent_granted=False,
            unsubscribed=True,
        ),
    )
    with pytest.raises(CampaignInvalidStateError, match="UNSUBSCRIBED"):
        await campaign_stack.service.authorize_send(organization.id, claim)
    assert await _permit_count(campaign_stack, organization.id) == 0
    assert campaign_stack.messaging.transport.requests == []


async def test_unsubscribe_denial_is_committed_before_service_error(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    _, run, (customer, identity, _) = await running_campaign(campaign_stack, organization.id)
    claim = await ready_claim(campaign_stack, organization.id, run)
    await campaign_stack.service.set_contact_preference(
        organization.id,
        ContactPreferenceRequest(
            customer_id=customer.id,
            identity_id=identity.id,
            channel="SMS",
            consent_granted=False,
            unsubscribed=True,
        ),
    )

    with pytest.raises(CampaignInvalidStateError, match="UNSUBSCRIBED"):
        await campaign_stack.service.authorize_send(organization.id, claim)

    async with campaign_stack.database.tenant_transaction(organization.id) as tenant:
        attempt = (
            await tenant.session.execute(
                select(CampaignRecipientAttemptRecord).where(
                    CampaignRecipientAttemptRecord.id == claim.attempt.id
                )
            )
        ).scalar_one()
        recipient = (
            await tenant.session.execute(
                select(CampaignRecipientRecord).where(
                    CampaignRecipientRecord.id == claim.recipient.id
                )
            )
        ).scalar_one()
        transitions = (
            (
                await tenant.session.execute(
                    select(CampaignTransitionHistoryRecord).where(
                        CampaignTransitionHistoryRecord.entity_id == claim.attempt.id,
                        CampaignTransitionHistoryRecord.reason_code == "UNSUBSCRIBED",
                    )
                )
            )
            .scalars()
            .all()
        )

    assert attempt.state == RecipientAttemptState.SUPPRESSED.value
    assert attempt.error_code == "UNSUBSCRIBED"
    assert recipient.state == RecipientState.SUPPRESSED.value
    assert recipient.eligibility_reason == "UNSUBSCRIBED"
    assert len(transitions) == 1


async def test_suppression_before_permit_blocks_authorization(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    campaign, run, (_, identity, _) = await running_campaign(campaign_stack, organization.id)
    claim = await ready_claim(campaign_stack, organization.id, run)
    await campaign_stack.service.add_suppression(
        organization.id,
        SuppressionRequest(
            channel="SMS",
            scope="IDENTITY",
            identity_id=identity.id,
            reason_code="RECIPIENT_OPT_OUT",
        ),
    )
    with pytest.raises(CampaignInvalidStateError, match="SUPPRESSED"):
        await campaign_stack.service.authorize_send(organization.id, claim)
    assert await _permit_count(campaign_stack, organization.id) == 0
    async with campaign_stack.database.tenant_transaction(organization.id) as tenant:
        attempt = (
            await tenant.session.execute(
                select(CampaignRecipientAttemptRecord).where(
                    CampaignRecipientAttemptRecord.id == claim.attempt.id
                )
            )
        ).scalar_one()
        recipient = (
            await tenant.session.execute(
                select(CampaignRecipientRecord).where(
                    CampaignRecipientRecord.id == claim.recipient.id
                )
            )
        ).scalar_one()
    assert attempt.state == RecipientAttemptState.SUPPRESSED.value
    assert attempt.error_code == "SUPPRESSED_GLOBAL"
    assert recipient.state == RecipientState.SUPPRESSED.value
    assert campaign.id == claim.run.campaign_id


async def test_cancel_before_permit_blocks_but_permit_before_cancel_remains_authorized(
    campaign_stack: Any, make_organization: Any
) -> None:
    first = await make_organization()
    campaign, run, _ = await running_campaign(campaign_stack, first.id)
    claim = await ready_claim(campaign_stack, first.id, run)
    await campaign_stack.service.cancel_campaign(first.id, campaign.id)
    with pytest.raises(CampaignExecutionFencedError):
        await campaign_stack.service.authorize_send(first.id, claim)
    async with campaign_stack.database.tenant_transaction(first.id) as tenant:
        cancelled_attempt = (
            await tenant.session.execute(
                select(CampaignRecipientAttemptRecord).where(
                    CampaignRecipientAttemptRecord.id == claim.attempt.id
                )
            )
        ).scalar_one()
    assert cancelled_attempt.state == RecipientAttemptState.CANCELLED.value
    assert cancelled_attempt.error_code == "CANCELLED"

    second = await make_organization()
    campaign, run, _ = await running_campaign(campaign_stack, second.id)
    claim = await ready_claim(campaign_stack, second.id, run)
    permit = await campaign_stack.service.authorize_send(second.id, claim)
    await campaign_stack.service.cancel_campaign(second.id, campaign.id)
    consumed = await campaign_stack.service.dispatch_send(second.id, permit.id)
    assert consumed.message_id is not None


async def test_quiet_hours_fail_closed_with_durable_next_eligibility(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    now = dt.datetime.now(dt.UTC)
    policy = QuietHoursPolicy(
        enabled=True,
        timezone="UTC",
        start_local=(now - dt.timedelta(minutes=1)).time().replace(tzinfo=None),
        end_local=(now + dt.timedelta(minutes=5)).time().replace(tzinfo=None),
    )
    draft, (customer, identity, _) = await campaign_draft(
        campaign_stack, organization.id, quiet_hours=policy
    )
    await campaign_stack.service.set_contact_preference(
        organization.id,
        ContactPreferenceRequest(
            customer_id=customer.id,
            identity_id=identity.id,
            channel="SMS",
            consent_granted=True,
            evidence_ref="consent:quiet-hours",
        ),
    )
    campaign = await campaign_stack.service.create_campaign(
        organization.id,
        CreateCampaignRequest(campaign_key="campaign.quiet", name="Quiet", draft=draft),
    )
    campaign = await campaign_stack.service.prepare_campaign(organization.id, campaign.id)
    recipients = await campaign_stack.service.audience(
        organization.id, campaign.id, limit=10, offset=0
    )
    assert recipients[0].eligibility_reason.value == "DEFERRED_QUIET_HOURS"
    assert recipients[0].next_eligible_at is not None


async def test_quiet_hours_final_denial_commits_defer_before_error(
    campaign_stack: Any, make_organization: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    organization = await make_organization()
    policy = QuietHoursPolicy(
        enabled=True,
        timezone="UTC",
        start_local=dt.time(21, 0),
        end_local=dt.time(8, 0),
    )
    draft, (customer, identity, _) = await campaign_draft(
        campaign_stack, organization.id, quiet_hours=policy
    )
    await campaign_stack.service.set_contact_preference(
        organization.id,
        ContactPreferenceRequest(
            customer_id=customer.id,
            identity_id=identity.id,
            channel="SMS",
            consent_granted=True,
            evidence_ref="consent:quiet-final",
        ),
    )
    original_database_now = CampaignRepository.database_now

    async def daytime(repo: CampaignRepository) -> dt.datetime:
        return dt.datetime(2026, 9, 14, 12, 0, tzinfo=dt.UTC)

    monkeypatch.setattr(CampaignRepository, "database_now", daytime)
    campaign = await campaign_stack.service.create_campaign(
        organization.id,
        CreateCampaignRequest(campaign_key="campaign.quiet-final", name="Quiet", draft=draft),
    )
    campaign = await campaign_stack.service.prepare_campaign(organization.id, campaign.id)
    run = await campaign_stack.service.start_campaign(organization.id, campaign.id)
    assert run.release_workflow_run_id is not None
    await campaign_stack.workflows.execute_next(
        campaign_principal(organization.id), run.release_workflow_run_id
    )
    run = await campaign_stack.service.confirm_release(organization.id, run.id)
    claim = await ready_claim(campaign_stack, organization.id, run)

    async def nighttime(repo: CampaignRepository) -> dt.datetime:
        return dt.datetime(2026, 9, 14, 22, 0, tzinfo=dt.UTC)

    monkeypatch.setattr(CampaignRepository, "database_now", nighttime)
    with pytest.raises(CampaignInvalidStateError, match="DEFERRED_QUIET_HOURS"):
        await campaign_stack.service.authorize_send(organization.id, claim)

    monkeypatch.setattr(CampaignRepository, "database_now", original_database_now)
    async with campaign_stack.database.tenant_transaction(organization.id) as tenant:
        attempt = (
            await tenant.session.execute(
                select(CampaignRecipientAttemptRecord).where(
                    CampaignRecipientAttemptRecord.id == claim.attempt.id
                )
            )
        ).scalar_one()
        recipient = (
            await tenant.session.execute(
                select(CampaignRecipientRecord).where(
                    CampaignRecipientRecord.id == claim.recipient.id
                )
            )
        ).scalar_one()
    assert attempt.state == RecipientAttemptState.READY_TO_SEND.value
    assert attempt.error_code == "DEFERRED_QUIET_HOURS"
    assert recipient.state == RecipientState.DEFERRED.value
    assert recipient.next_eligible_at == dt.datetime(2026, 9, 15, 8, 0, tzinfo=dt.UTC)


async def test_throttle_policy_is_bounded_and_fail_closed() -> None:
    with pytest.raises(ValueError):
        ThrottlePolicy(messages_per_minute=0)
    with pytest.raises(ValueError):
        ThrottlePolicy(recipients_per_tick=501)
