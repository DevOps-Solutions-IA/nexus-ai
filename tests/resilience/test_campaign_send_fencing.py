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
from nexus_ai.domain.campaigns.models import CampaignSendPermitRecord
from tests.integration.test_campaign_service import (
    campaign_draft,
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


async def test_throttle_policy_is_bounded_and_fail_closed() -> None:
    with pytest.raises(ValueError):
        ThrottlePolicy(messages_per_minute=0)
    with pytest.raises(ValueError):
        ThrottlePolicy(recipients_per_tick=501)
