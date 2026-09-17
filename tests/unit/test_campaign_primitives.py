"""Pure Campaign contracts, state transitions and deterministic identity."""

import datetime as dt
import uuid

import pytest
from pydantic import ValidationError

from nexus_ai.campaigns.entities import (
    CampaignDraft,
    ContactPreferenceRequest,
    CreateCampaignRequest,
    QuietHoursPolicy,
    SuppressionRequest,
)
from nexus_ai.campaigns.errors import CampaignInvalidStateError
from nexus_ai.campaigns.identity import downstream_key, recipient_identity, semantic_digest
from nexus_ai.campaigns.state_machine import (
    CampaignState,
    RecipientAttemptState,
    require_campaign_transition,
    resolve_attempt_transition,
)
from nexus_ai.messaging.entities import MessageContent


def _draft(**changes):
    values = {
        "release_workflow_version_id": uuid.uuid4(),
        "recipient_workflow_version_id": uuid.uuid4(),
        "account_id": uuid.uuid4(),
        "channel": "SMS",
        "content": {"text": "hello"},
        "audience": [
            {
                "customer_id": uuid.uuid4(),
                "identity_id": uuid.uuid4(),
                "conversation_id": uuid.uuid4(),
            }
        ],
    }
    values.update(changes)
    return CampaignDraft.model_validate(values)


def test_campaign_contracts_are_strict_bounded_and_provider_neutral() -> None:
    draft = _draft()
    assert draft.content == MessageContent(text="hello")
    request = CreateCampaignRequest(campaign_key="renewal.2026", name="Renewal", draft=draft)
    assert request.draft.channel.value == "SMS"
    with pytest.raises(ValidationError):
        CampaignDraft.model_validate({**draft.model_dump(), "url": "https://attacker.test"})
    with pytest.raises(ValidationError):
        CampaignDraft.model_validate({**draft.model_dump(), "subject": "not SMS"})
    member = draft.audience[0]
    with pytest.raises(ValidationError):
        _draft(audience=[member, member])
    with pytest.raises(ValidationError):
        _draft(audience=[])


def test_governed_sources_and_suppression_scope_are_closed() -> None:
    with pytest.raises(ValidationError):
        _draft(audience_source="RAW_SQL", audience=())
    with pytest.raises(ValidationError):
        SuppressionRequest(channel="SMS", scope="CAMPAIGN", reason_code="OPT_OUT")
    valid = SuppressionRequest(channel="SMS", scope="GLOBAL", reason_code="LEGAL_BLOCK")
    assert valid.scope.value == "GLOBAL"
    with pytest.raises(ValidationError):
        SuppressionRequest(
            channel="SMS",
            scope="GLOBAL",
            customer_id=uuid.uuid4(),
            reason_code="LEGAL_BLOCK",
        )
    with pytest.raises(ValidationError):
        SuppressionRequest(
            channel="SMS",
            scope="CUSTOMER",
            customer_id=uuid.uuid4(),
            identity_id=uuid.uuid4(),
            reason_code="LEGAL_BLOCK",
        )


def test_quiet_hours_require_iana_timezone_and_naive_wall_times() -> None:
    assert QuietHoursPolicy(enabled=True, timezone="America/Bogota").enabled
    with pytest.raises(ValidationError):
        QuietHoursPolicy(enabled=True, timezone="Mars/Olympus")
    with pytest.raises(ValidationError):
        QuietHoursPolicy(start_local=dt.time(21, tzinfo=dt.UTC))


def test_affirmative_consent_requires_evidence_and_withdrawal_fails_closed() -> None:
    values = {"customer_id": uuid.uuid4(), "identity_id": uuid.uuid4(), "channel": "SMS"}
    with pytest.raises(ValidationError):
        ContactPreferenceRequest(**values, consent_granted=True)
    with pytest.raises(ValidationError):
        ContactPreferenceRequest(
            **values,
            consent_granted=True,
            unsubscribed=True,
            evidence_ref="consent:invalid",
        )


def test_state_machines_are_explicit_and_terminal_absorbing() -> None:
    require_campaign_transition(CampaignState.DRAFT, CampaignState.PREPARING)
    require_campaign_transition(CampaignState.RUNNING, CampaignState.PAUSED)
    with pytest.raises(CampaignInvalidStateError):
        require_campaign_transition(CampaignState.CANCELLED, CampaignState.RUNNING)
    resolve_attempt_transition(
        RecipientAttemptState.READY_TO_SEND, RecipientAttemptState.DISPATCH_AUTHORIZED
    )
    with pytest.raises(CampaignInvalidStateError):
        resolve_attempt_transition(
            RecipientAttemptState.DISPATCHED, RecipientAttemptState.READY_TO_SEND
        )


def test_identities_are_stable_bounded_and_semantically_separate() -> None:
    values = [uuid.uuid4() for _ in range(4)]
    first = recipient_identity(values[0], values[1], 3, values[2], values[3], "SMS")
    second = recipient_identity(values[0], values[1], 3, values[2], uuid.uuid4(), "SMS")
    assert len(first) == 64
    assert first != second
    assert downstream_key("workflow", first) != downstream_key("message", first)
    assert semantic_digest({"b": 2, "a": 1}) == semantic_digest({"a": 1, "b": 2})
