"""P17 closed contracts, state absorption and stable identities."""

import uuid

import pytest
from pydantic import ValidationError

from nexus_ai.events.registry import EVENT_REGISTRY
from nexus_ai.humans.entities import (
    AssignmentState,
    CreateQueueRequest,
    HumanChannel,
    HumanSendRequest,
    PresenceState,
    SetPresenceRequest,
    WorkItemState,
)
from nexus_ai.humans.errors import HumanInvalidStateError
from nexus_ai.humans.identity import downstream_key, semantic_digest, token_digest
from nexus_ai.humans.state_machine import require_assignment_transition, require_work_transition


def test_work_terminal_states_are_absorbing() -> None:
    for state in (WorkItemState.COMPLETED, WorkItemState.CANCELLED):
        with pytest.raises(HumanInvalidStateError):
            require_work_transition(state, WorkItemState.QUEUED)


def test_assignment_terminal_states_are_absorbing() -> None:
    for state in (
        AssignmentState.RELEASED,
        AssignmentState.TRANSFERRED,
        AssignmentState.COMPLETED,
    ):
        with pytest.raises(HumanInvalidStateError):
            require_assignment_transition(state, AssignmentState.ACTIVE)


def test_normal_work_lifecycle_is_legal() -> None:
    require_work_transition(WorkItemState.QUEUED, WorkItemState.CLAIMED)
    require_work_transition(WorkItemState.CLAIMED, WorkItemState.ACCEPTED)
    require_work_transition(WorkItemState.ACCEPTED, WorkItemState.ACTIVE)
    require_work_transition(WorkItemState.ACTIVE, WorkItemState.WRAP_UP)
    require_work_transition(WorkItemState.WRAP_UP, WorkItemState.COMPLETED)


def test_queue_requires_bounded_unique_channels() -> None:
    with pytest.raises(ValidationError):
        CreateQueueRequest(
            queue_key="support",
            name="Support",
            supported_channels=(HumanChannel.SMS, HumanChannel.SMS),
        )


def test_offline_presence_requires_zero_capacity() -> None:
    with pytest.raises(ValidationError):
        SetPresenceRequest(state=PresenceState.OFFLINE, capacity=1)


def test_human_send_rejects_voice_transport() -> None:
    with pytest.raises(ValidationError):
        HumanSendRequest(
            claim_token="x" * 32,
            lease_version=1,
            ownership_generation=1,
            account_id=uuid.uuid7(),
            to=("+14155550100",),
            content="hello",
            channel=HumanChannel.VOICE,
            idempotency_key="human:voice:test",
        )


def test_human_send_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        HumanSendRequest.model_validate(
            {
                "claim_token": "x" * 32,
                "lease_version": 1,
                "ownership_generation": 1,
                "account_id": str(uuid.uuid7()),
                "to": ["+14155550100"],
                "content": "hello",
                "channel": "SMS",
                "idempotency_key": "human:send:test",
                "provider_url": "https://attacker.invalid",
            }
        )


def test_stable_hashes_are_deterministic_and_non_secret() -> None:
    assert semantic_digest({"b": 2, "a": 1}) == semantic_digest({"a": 1, "b": 2})
    assert downstream_key("p09-send", "semantic:key") == downstream_key("p09-send", "semantic:key")
    assert token_digest("opaque-secret") != "opaque-secret"


def test_human_event_contracts_are_registered() -> None:
    for event_type in (
        "human.work.queued",
        "human.work.claimed",
        "human.work.accepted",
        "human.work.transferred",
        "human.work.requeued",
        "human.work.completed",
        "human.work.cancelled",
        "human.handoff.ai_to_human",
        "human.handoff.human_to_ai",
        "human.presence.changed",
        "human.supervisor.released",
    ):
        assert EVENT_REGISTRY.is_known_type(event_type)
