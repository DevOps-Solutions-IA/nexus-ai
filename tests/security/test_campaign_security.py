"""P16 fail-closed campaign attack surface and tenant-boundary assertions."""

import uuid
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from nexus_ai.campaigns.entities import CampaignDraft, SuppressionRequest
from nexus_ai.campaigns.errors import CampaignExecutionFencedError, CampaignNotFoundError
from nexus_ai.domain.campaigns.models import CampaignRecipientRecord
from tests.integration.test_campaign_service import prepared_campaign, running_campaign

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("url", "https://attacker.test"),
        ("sql", "select * from customers"),
        ("command", "rm -rf /"),
        ("python", "__import__('os')"),
        ("provider", "twilio"),
        ("credential", "secret"),
    ],
)
def test_campaign_draft_has_no_execution_escape_hatch(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        CampaignDraft.model_validate(
            {
                "release_workflow_version_id": str(uuid.uuid4()),
                "recipient_workflow_version_id": str(uuid.uuid4()),
                "account_id": str(uuid.uuid4()),
                "channel": "SMS",
                "content": {"text": "safe"},
                "audience": [
                    {
                        "customer_id": str(uuid.uuid4()),
                        "identity_id": str(uuid.uuid4()),
                        "conversation_id": str(uuid.uuid4()),
                    }
                ],
                field: value,
            }
        )


def test_suppression_scope_cannot_smuggle_unrelated_references() -> None:
    with pytest.raises(ValidationError):
        SuppressionRequest(
            channel="SMS",
            scope="CAMPAIGN",
            campaign_id=uuid.uuid4(),
            identity_id=uuid.uuid4(),
            reason_code="ABUSE_BLOCK",
        )


async def test_cross_tenant_campaign_and_repository_access_is_invisible(
    campaign_stack: Any, make_organization: Any
) -> None:
    first, second = await make_organization(), await make_organization()
    campaign, _ = await prepared_campaign(campaign_stack, first.id)
    with pytest.raises(CampaignNotFoundError):
        await campaign_stack.service.get_campaign(second.id, campaign.id)
    assert await campaign_stack.service.list_campaigns(second.id, limit=10, offset=0) == []
    async with campaign_stack.database.tenant_transaction(second.id) as tenant:
        visible = (
            await tenant.session.execute(
                text("SELECT count(*) FROM campaigns WHERE id = :id"), {"id": campaign.id}
            )
        ).scalar_one()
        assert visible == 0


async def test_cross_tenant_campaign_recipient_fk_is_database_backstop(
    campaign_stack: Any, make_organization: Any
) -> None:
    first, second = await make_organization(), await make_organization()
    campaign, _ = await prepared_campaign(campaign_stack, first.id)
    recipients = await campaign_stack.service.audience(first.id, campaign.id, limit=10, offset=0)
    source = recipients[0]
    with pytest.raises(DBAPIError):
        async with campaign_stack.database.tenant_transaction(second.id) as tenant:
            tenant.session.add(
                CampaignRecipientRecord(
                    id=uuid.uuid7(),
                    organization_id=second.id,
                    snapshot_id=source.snapshot_id,
                    customer_id=source.customer_id,
                    identity_id=source.identity_id,
                    conversation_id=source.conversation_id,
                    channel=source.channel.value,
                    destination_fingerprint=source.destination_fingerprint,
                    state="ELIGIBLE",
                    eligibility_reason="ELIGIBLE",
                    evaluated_consent_epoch=1,
                    evaluated_suppression_epoch=1,
                )
            )
            await tenant.session.flush()


async def test_forged_or_stale_claim_cannot_authorize_send(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    _, run, _ = await running_campaign(campaign_stack, organization.id)
    claim = await campaign_stack.service.claim_recipient(organization.id, run.id, uuid.uuid7())
    assert claim is not None
    stale = claim.model_copy(update={"claim_token": uuid.uuid7()})
    with pytest.raises(CampaignExecutionFencedError):
        await campaign_stack.service.start_recipient_workflow(organization.id, stale)
