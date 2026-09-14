"""Real PostgreSQL multi-worker Campaign claim tests."""

import asyncio
import uuid
from typing import Any

import pytest

from nexus_ai.campaigns.entities import ContactPreferenceRequest, CreateCampaignRequest
from nexus_ai.campaigns.errors import (
    CampaignConflictError,
    CampaignInvalidStateError,
)
from nexus_ai.campaigns.state_machine import CampaignState
from tests.integration.test_campaign_service import (
    prepared_campaign,
    ready_claim,
    running_campaign,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_two_workers_claim_one_recipient(campaign_stack: Any, make_organization: Any) -> None:
    organization = await make_organization()
    _, run, _ = await running_campaign(campaign_stack, organization.id)
    first, second = await asyncio.gather(
        campaign_stack.service.claim_recipient(organization.id, run.id, uuid.uuid7()),
        campaign_stack.service.claim_recipient(organization.id, run.id, uuid.uuid7()),
    )
    assert len([claim for claim in (first, second) if claim is not None]) == 1


async def test_concurrent_same_campaign_key_has_one_definition(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    prepared, _ = await prepared_campaign(campaign_stack, organization.id)
    payload = CreateCampaignRequest(
        campaign_key="campaign.concurrent", name="Race", draft=prepared.draft
    )
    results = await asyncio.gather(
        campaign_stack.service.create_campaign(organization.id, payload),
        campaign_stack.service.create_campaign(organization.id, payload),
        return_exceptions=True,
    )
    assert len([item for item in results if not isinstance(item, BaseException)]) == 1
    assert len([item for item in results if isinstance(item, CampaignConflictError)]) == 1


async def test_duplicate_campaign_start_reuses_one_logical_run(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    campaign, _ = await prepared_campaign(campaign_stack, organization.id)
    first, second = await asyncio.gather(
        campaign_stack.service.start_campaign(
            organization.id, campaign.id, idempotency_key="campaign:duplicate-wake"
        ),
        campaign_stack.service.start_campaign(
            organization.id, campaign.id, idempotency_key="campaign:duplicate-wake"
        ),
    )
    assert first.id == second.id
    assert first.release_workflow_run_id == second.release_workflow_run_id


async def test_cancel_and_claim_have_one_durable_serialization_order(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    campaign, run, _ = await running_campaign(campaign_stack, organization.id)
    claim_result, cancelled = await asyncio.gather(
        campaign_stack.service.claim_recipient(organization.id, run.id, uuid.uuid7()),
        campaign_stack.service.cancel_campaign(organization.id, campaign.id),
    )
    assert cancelled.state is CampaignState.CANCELLED
    assert claim_result is None or claim_result.attempt.state.value == "CLAIMED"
    assert (
        await campaign_stack.service.claim_recipient(organization.id, run.id, uuid.uuid7()) is None
    )


async def test_unsubscribe_and_final_permit_follow_commit_order(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    _, run, (customer, identity, _) = await running_campaign(campaign_stack, organization.id)
    claim = await ready_claim(campaign_stack, organization.id, run)
    permit_result, epoch = await asyncio.gather(
        campaign_stack.service.authorize_send(organization.id, claim),
        campaign_stack.service.set_contact_preference(
            organization.id,
            ContactPreferenceRequest(
                customer_id=customer.id,
                identity_id=identity.id,
                channel="SMS",
                consent_granted=False,
                unsubscribed=True,
            ),
        ),
        return_exceptions=True,
    )
    assert isinstance(epoch, int)
    if isinstance(permit_result, BaseException):
        assert isinstance(permit_result, CampaignInvalidStateError)
    else:
        assert epoch > permit_result.consent_epoch
