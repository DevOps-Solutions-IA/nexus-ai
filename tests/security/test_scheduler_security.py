"""P15 attack-surface and tenant-boundary security assertions."""

import datetime as dt
from uuid import uuid4

import pytest
from pydantic import ValidationError

from nexus_ai.scheduler.entities import CreateScheduleRequest, RecurrenceSpec


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_type", "SHELL"),
        ("target_type", "SQL"),
        ("target_type", "ARBITRARY_HTTP"),
        ("target_type", "PYTHON"),
        ("target_type", "TOOL_DIRECT"),
        ("target_type", "AGENT_DIRECT"),
        ("provider", "openai"),
        ("authorization", "Bearer secret"),
    ],
)
def test_scheduler_has_no_direct_execution_surface(field: str, value: str) -> None:
    payload = {
        "schedule_key": "security.schedule",
        "workflow_version_id": str(uuid4()),
        "schedule_type": "ONE_TIME",
        "timezone": "UTC",
        "start_at": dt.datetime.now(dt.UTC).isoformat(),
        field: value,
    }
    with pytest.raises(ValidationError):
        CreateScheduleRequest.model_validate(payload)


def test_recurrence_rejects_unbounded_or_expression_input() -> None:
    with pytest.raises(ValidationError):
        RecurrenceSpec.model_validate({"frequency": "MINUTELY", "interval": 10001})
    with pytest.raises(ValidationError):
        RecurrenceSpec.model_validate(
            {"frequency": "MINUTELY", "cron": "* * * * *", "expression": "__import__('os')"}
        )


def test_oversized_workflow_input_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CreateScheduleRequest(
            schedule_key="security.large",
            workflow_version_id=uuid4(),
            schedule_type="ONE_TIME",
            timezone="UTC",
            start_at=dt.datetime.now(dt.UTC),
            input={"payload": "x" * 70_000},
        )
