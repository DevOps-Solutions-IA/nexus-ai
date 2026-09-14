"""P16 Campaign API and P04 event contracts."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from nexus_ai.application import create_app
from nexus_ai.campaigns import events as campaign_events  # noqa: F401
from nexus_ai.campaigns.entities import (
    CampaignDraft,
    CreateCampaignRequest,
    ScheduleCampaignRequest,
)
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.errors import EventContractError
from nexus_ai.events.registry import EVENT_REGISTRY


def test_openapi_exposes_only_governed_campaign_surface() -> None:
    paths = set(create_app().openapi()["paths"])
    expected = {
        "/api/v1/campaigns",
        "/api/v1/campaigns/{campaign_id}",
        "/api/v1/campaigns/{campaign_id}/prepare",
        "/api/v1/campaigns/{campaign_id}/schedule",
        "/api/v1/campaigns/{campaign_id}/start",
        "/api/v1/campaigns/{campaign_id}/pause",
        "/api/v1/campaigns/{campaign_id}/resume",
        "/api/v1/campaigns/{campaign_id}/cancel",
        "/api/v1/campaigns/{campaign_id}/audience",
        "/api/v1/campaigns/{campaign_id}/recipients",
        "/api/v1/campaigns/{campaign_id}/runs",
        "/api/v1/campaigns/{campaign_id}/transitions",
    }
    assert expected <= paths
    forbidden = ("/shell", "/sql", "/python", "/provider", "/arbitrary-http")
    assert not any(token in path for path in paths for token in forbidden)


@pytest.mark.parametrize(
    "field",
    [
        "organization_id",
        "url",
        "sql",
        "command",
        "python",
        "provider_credentials",
        "tool_key",
        "agent_id",
    ],
)
def test_campaign_request_rejects_spoofing_and_direct_execution(field: str) -> None:
    member = {
        "customer_id": str(uuid4()),
        "identity_id": str(uuid4()),
        "conversation_id": str(uuid4()),
    }
    draft = {
        "release_workflow_version_id": str(uuid4()),
        "recipient_workflow_version_id": str(uuid4()),
        "account_id": str(uuid4()),
        "channel": "SMS",
        "content": {"text": "safe"},
        "audience": [member],
        field: "forbidden",
    }
    with pytest.raises(ValidationError):
        CampaignDraft.model_validate(draft)


def test_create_contract_rejects_authoritative_tenant_field() -> None:
    with pytest.raises(ValidationError):
        CreateCampaignRequest.model_validate(
            {"campaign_key": "campaign.safe", "name": "Safe", "organization_id": str(uuid4())}
        )


def test_schedule_contract_rejects_caller_controlled_schedule_binding() -> None:
    with pytest.raises(ValidationError):
        ScheduleCampaignRequest.model_validate(
            {
                "schedule_id": str(uuid4()),
                "schedule_type": "ONE_TIME",
                "timezone": "UTC",
                "start_at": "2030-01-01T00:00:00Z",
            }
        )


@pytest.mark.parametrize(
    "event_type",
    [
        "campaign.created",
        "campaign.prepared",
        "campaign.scheduled",
        "campaign.started",
        "campaign.paused",
        "campaign.resumed",
        "campaign.cancelled",
        "campaign.completed",
        "campaign.failed",
        "campaign.recipient.eligible",
        "campaign.recipient.suppressed",
        "campaign.recipient.claimed",
        "campaign.recipient.workflow_started",
        "campaign.recipient.dispatched",
        "campaign.recipient.failed",
    ],
)
def test_campaign_events_are_registered(event_type: str) -> None:
    assert EVENT_REGISTRY.supported_versions(event_type) == (1,)


def test_campaign_event_rejects_secret_or_customer_payload() -> None:
    envelope = EventEnvelope.create(
        event_type="campaign.created",
        event_version=1,
        aggregate_type="campaign",
        aggregate_id=str(uuid4()),
        organization_id=uuid4(),
        producer="nexus-ai",
        payload={"campaign_id": str(uuid4()), "state": "DRAFT", "message_body": "secret"},
    )
    with pytest.raises(EventContractError):
        EVENT_REGISTRY.decode(envelope)
