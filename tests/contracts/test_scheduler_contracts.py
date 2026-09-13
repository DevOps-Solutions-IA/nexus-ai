"""Scheduler API and P04 event contracts."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from nexus_ai.application import create_app
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.errors import EventContractError
from nexus_ai.events.registry import EVENT_REGISTRY
from nexus_ai.scheduler import events as scheduler_events  # noqa: F401
from nexus_ai.scheduler.entities import CreateScheduleRequest


def test_openapi_exposes_scheduler_without_executable_escape_hatches() -> None:
    paths = set(create_app().openapi()["paths"])
    assert {
        "/api/v1/schedules",
        "/api/v1/schedules/{schedule_id}",
        "/api/v1/schedules/{schedule_id}/activate",
        "/api/v1/schedules/{schedule_id}/pause",
        "/api/v1/schedules/{schedule_id}/resume",
        "/api/v1/schedules/{schedule_id}/cancel",
        "/api/v1/schedules/{schedule_id}/occurrences",
        "/api/v1/schedule-occurrences/{occurrence_id}",
    } <= paths
    assert not any(token in path for path in paths for token in ("/cron", "/shell", "/sql"))


def test_scheduler_request_rejects_spoofing_and_direct_targets() -> None:
    base = {
        "schedule_key": "safe.schedule",
        "workflow_version_id": str(uuid4()),
        "schedule_type": "ONE_TIME",
        "timezone": "UTC",
        "start_at": "2026-09-15T10:00:00Z",
    }
    for extra in ("organization_id", "url", "tool_key", "agent_id", "command", "sql"):
        with pytest.raises(ValidationError):
            CreateScheduleRequest.model_validate({**base, extra: "forbidden"})


@pytest.mark.parametrize(
    "event_type",
    [
        "scheduler.schedule.created",
        "scheduler.schedule.activated",
        "scheduler.schedule.paused",
        "scheduler.schedule.resumed",
        "scheduler.schedule.cancelled",
        "scheduler.occurrence.created",
        "scheduler.occurrence.claimed",
        "scheduler.occurrence.dispatched",
        "scheduler.occurrence.failed",
        "scheduler.occurrence.skipped",
        "scheduler.occurrence.cancelled",
    ],
)
def test_scheduler_events_are_registered(event_type: str) -> None:
    assert EVENT_REGISTRY.supported_versions(event_type) == (1,)


def test_scheduler_event_rejects_secret_payload() -> None:
    envelope = EventEnvelope.create(
        event_type="scheduler.schedule.created",
        event_version=1,
        aggregate_type="scheduler_schedule",
        aggregate_id=str(uuid4()),
        organization_id=uuid4(),
        producer="nexus-ai",
        payload={"schedule_id": str(uuid4()), "state": "DRAFT", "api_key": "secret"},
    )
    with pytest.raises(EventContractError):
        EVENT_REGISTRY.decode(envelope)
