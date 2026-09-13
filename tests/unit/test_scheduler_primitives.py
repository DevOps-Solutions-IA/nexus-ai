"""NXS-P15 state, recurrence, validation and terminal-absorption contracts."""

import datetime as dt
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from nexus_ai.scheduler.entities import (
    CreateScheduleRequest,
    MisfirePolicy,
    RecurrenceFrequency,
    RecurrenceSpec,
    ScheduleType,
)
from nexus_ai.scheduler.errors import ScheduleInvalidRecurrenceError, ScheduleInvalidStateError
from nexus_ai.scheduler.identity import build_occurrence_key
from nexus_ai.scheduler.recurrence import first_slot, next_slot, timezone_data_version
from nexus_ai.scheduler.state_machine import (
    OccurrenceState,
    ScheduleState,
    require_occurrence_transition,
    require_schedule_transition,
)

UTC = dt.UTC


def _request(**overrides: object) -> CreateScheduleRequest:
    values: dict[str, object] = {
        "schedule_key": "billing.followup",
        "workflow_version_id": uuid4(),
        "schedule_type": "ONE_TIME",
        "timezone": "America/Bogota",
        "start_at": dt.datetime(2026, 9, 15, 13, tzinfo=UTC),
    }
    values.update(overrides)
    return CreateScheduleRequest.model_validate(values)


def test_only_start_workflow_and_strict_input_are_accepted() -> None:
    with pytest.raises(ValidationError):
        _request(target_type="SHELL")
    with pytest.raises(ValidationError):
        _request(url="https://attacker.invalid")
    with pytest.raises(ValidationError):
        _request(organization_id=uuid4())


@pytest.mark.parametrize("timezone", ["No/Such_Zone", "", "../../etc/passwd"])
def test_invalid_timezones_fail_closed(timezone: str) -> None:
    with pytest.raises(ValidationError):
        _request(timezone=timezone)


def test_recurrence_contract_is_closed_and_bounded() -> None:
    weekly = RecurrenceSpec(
        frequency=RecurrenceFrequency.WEEKLY,
        interval=2,
        weekdays=(0, 4),
        local_time=dt.time(8),
    )
    assert weekly.interval == 2
    with pytest.raises(ValidationError):
        RecurrenceSpec.model_validate({"frequency": "CRON", "expression": "* * * * *"})
    with pytest.raises(ValidationError):
        RecurrenceSpec(frequency=RecurrenceFrequency.MINUTELY, interval=0)
    with pytest.raises(ValidationError):
        RecurrenceSpec(
            frequency=RecurrenceFrequency.MONTHLY,
            month_days=(1,),
            local_time=dt.time(8),
            weekdays=(1,),
        )


def test_schedule_shape_and_catch_up_bounds() -> None:
    recurrence = RecurrenceSpec(frequency=RecurrenceFrequency.MINUTELY)
    recurring = _request(
        schedule_type=ScheduleType.RECURRING,
        recurrence=recurrence,
        misfire_policy=MisfirePolicy.CATCH_UP_BOUNDED,
        max_catch_up=100,
    )
    assert recurring.max_catch_up == 100
    with pytest.raises(ValidationError):
        _request(schedule_type=ScheduleType.RECURRING)
    with pytest.raises(ValidationError):
        _request(max_catch_up=2)


def test_bogota_and_minutely_slots_are_utc_authoritative() -> None:
    start = dt.datetime(2026, 9, 15, 13, tzinfo=UTC)
    first = first_slot(start_at=start, timezone="America/Bogota", recurrence=None)
    assert first.intended_local_time.hour == 8
    assert first.scheduled_for == start
    recurrence = RecurrenceSpec(frequency=RecurrenceFrequency.MINUTELY, interval=5)
    second = next_slot(first.intended_local_time, first.scheduled_for, "America/Bogota", recurrence)
    assert second.scheduled_for == start + dt.timedelta(minutes=5)
    assert timezone_data_version()


def test_hourly_daily_weekly_and_monthly_recurrence_is_deterministic() -> None:
    previous = dt.datetime(2026, 1, 1, 8)
    hourly = next_slot(
        previous,
        dt.datetime(2026, 1, 1, 8, tzinfo=UTC),
        "UTC",
        RecurrenceSpec(frequency=RecurrenceFrequency.HOURLY, interval=2),
    )
    assert hourly.intended_local_time == dt.datetime(2026, 1, 1, 10)

    daily = first_slot(
        start_at=dt.datetime(2026, 1, 1, 12, tzinfo=UTC),
        timezone="UTC",
        recurrence=RecurrenceSpec(
            frequency=RecurrenceFrequency.DAILY, interval=2, local_time=dt.time(8)
        ),
    )
    assert daily.intended_local_time == dt.datetime(2026, 1, 3, 8)

    weekly = next_slot(
        dt.datetime(2026, 1, 5, 8),
        dt.datetime(2026, 1, 5, 8, tzinfo=UTC),
        "UTC",
        RecurrenceSpec(
            frequency=RecurrenceFrequency.WEEKLY,
            weekdays=(0, 4),
            local_time=dt.time(8),
        ),
    )
    assert weekly.intended_local_time == dt.datetime(2026, 1, 9, 8)

    monthly = next_slot(
        dt.datetime(2026, 1, 31, 8),
        dt.datetime(2026, 1, 31, 8, tzinfo=UTC),
        "UTC",
        RecurrenceSpec(
            frequency=RecurrenceFrequency.MONTHLY,
            month_days=(31,),
            local_time=dt.time(8),
        ),
    )
    assert monthly.intended_local_time == dt.datetime(2026, 3, 31, 8)


def test_first_calendar_slot_honors_weekday_and_month_day_selectors() -> None:
    weekly = first_slot(
        start_at=dt.datetime(2026, 1, 6, 7, tzinfo=UTC),
        timezone="UTC",
        recurrence=RecurrenceSpec(
            frequency=RecurrenceFrequency.WEEKLY,
            weekdays=(0,),
            local_time=dt.time(8),
        ),
    )
    assert weekly.intended_local_time == dt.datetime(2026, 1, 12, 8)

    monthly = first_slot(
        start_at=dt.datetime(2026, 1, 10, 7, tzinfo=UTC),
        timezone="UTC",
        recurrence=RecurrenceSpec(
            frequency=RecurrenceFrequency.MONTHLY,
            month_days=(15,),
            local_time=dt.time(8),
        ),
    )
    assert monthly.intended_local_time == dt.datetime(2026, 1, 15, 8)


def test_monthly_leap_day_and_recurrence_search_bound_are_deterministic() -> None:
    leap_slot = first_slot(
        start_at=dt.datetime(2028, 1, 30, 9, tzinfo=UTC),
        timezone="UTC",
        recurrence=RecurrenceSpec(
            frequency=RecurrenceFrequency.MONTHLY,
            month_days=(29,),
            local_time=dt.time(8),
        ),
    )
    assert leap_slot.intended_local_time == dt.datetime(2028, 2, 29, 8)

    with pytest.raises(ScheduleInvalidRecurrenceError):
        next_slot(
            dt.datetime(2026, 1, 31, 8),
            dt.datetime(2026, 1, 31, 8, tzinfo=UTC),
            "UTC",
            RecurrenceSpec(
                frequency=RecurrenceFrequency.MONTHLY,
                interval=10_000,
                month_days=(31,),
                local_time=dt.time(8),
            ),
        )


def test_calendar_contract_rejects_aware_local_time_and_duplicate_selectors() -> None:
    with pytest.raises(ValidationError):
        RecurrenceSpec(
            frequency=RecurrenceFrequency.DAILY,
            local_time=dt.time(8, tzinfo=dt.UTC),
        )
    with pytest.raises(ValidationError):
        RecurrenceSpec(
            frequency=RecurrenceFrequency.WEEKLY,
            weekdays=(1, 1),
            local_time=dt.time(8),
        )
    with pytest.raises(ValidationError):
        RecurrenceSpec(
            frequency=RecurrenceFrequency.MONTHLY,
            month_days=(15, 15),
            local_time=dt.time(8),
        )


def test_dst_gap_is_explicit_and_fall_back_uses_fold_zero_once() -> None:
    daily = RecurrenceSpec(frequency=RecurrenceFrequency.DAILY, local_time=dt.time(2, 30))
    gap = first_slot(
        start_at=dt.datetime(2026, 3, 8, 5, tzinfo=UTC),
        timezone="America/New_York",
        recurrence=daily,
    )
    assert gap.intended_local_time == dt.datetime(2026, 3, 8, 2, 30)
    assert gap.nonexistent is True
    fold_daily = RecurrenceSpec(frequency=RecurrenceFrequency.DAILY, local_time=dt.time(1, 30))
    folded = first_slot(
        start_at=dt.datetime(2026, 11, 1, 4, tzinfo=UTC),
        timezone="America/New_York",
        recurrence=fold_daily,
    )
    assert folded.fold == 0
    assert folded.scheduled_for == dt.datetime(2026, 11, 1, 5, 30, tzinfo=UTC)

    schedule_id = UUID("018f4db8-1f5a-7abc-8def-0123456789ab")
    gap_key = build_occurrence_key(
        schedule_id=schedule_id,
        schedule_revision=4,
        timezone="America/New_York",
        intended_local_time=gap.intended_local_time,
        fold=gap.fold,
    )
    assert gap_key == build_occurrence_key(
        schedule_id=schedule_id,
        schedule_revision=4,
        timezone="America/New_York",
        intended_local_time=gap.intended_local_time,
        fold=gap.fold,
    )
    fold_key = build_occurrence_key(
        schedule_id=schedule_id,
        schedule_revision=4,
        timezone="America/New_York",
        intended_local_time=folded.intended_local_time,
        fold=folded.fold,
    )
    assert fold_key == build_occurrence_key(
        schedule_id=schedule_id,
        schedule_revision=4,
        timezone="America/New_York",
        intended_local_time=folded.intended_local_time,
        fold=0,
    )


def test_occurrence_identity_is_revision_timezone_and_fold_safe() -> None:
    schedule_id = UUID("018f4db8-1f5a-7abc-8def-0123456789ab")
    intended = dt.datetime(2026, 9, 13, 10, 0)

    def key(*, revision: int = 7, timezone: str = "America/Bogota", fold: int = 0) -> str:
        return build_occurrence_key(
            schedule_id=schedule_id,
            schedule_revision=revision,
            timezone=timezone,
            intended_local_time=intended,
            fold=fold,
        )

    assert key() == key()
    assert key() != key(revision=8)
    assert key() != key(timezone="Etc/GMT+5")
    assert key() != key(fold=1)
    assert key().startswith("v1:r7:f0:")
    assert len(key()) <= 160


@pytest.mark.parametrize(
    ("revision", "intended", "fold", "message"),
    (
        (0, dt.datetime(2026, 9, 13, 10, 0), 0, "schedule_revision must be positive"),
        (
            1,
            dt.datetime(2026, 9, 13, 10, 0, tzinfo=UTC),
            0,
            "intended_local_time must be a naive local wall-clock value",
        ),
        (1, dt.datetime(2026, 9, 13, 10, 0), 2, "fold must be 0 or 1"),
    ),
)
def test_occurrence_identity_rejects_invalid_canonical_components(
    revision: int, intended: dt.datetime, fold: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        build_occurrence_key(
            schedule_id=uuid4(),
            schedule_revision=revision,
            timezone="UTC",
            intended_local_time=intended,
            fold=fold,
        )


@pytest.mark.parametrize("terminal", [ScheduleState.COMPLETED, ScheduleState.CANCELLED])
def test_schedule_terminal_states_absorb(terminal: ScheduleState) -> None:
    with pytest.raises(ScheduleInvalidStateError):
        require_schedule_transition(terminal, ScheduleState.ACTIVE)


@pytest.mark.parametrize(
    "terminal",
    [
        OccurrenceState.DISPATCHED,
        OccurrenceState.FAILED,
        OccurrenceState.SKIPPED,
        OccurrenceState.CANCELLED,
    ],
)
def test_occurrence_terminal_states_absorb(terminal: OccurrenceState) -> None:
    with pytest.raises(ScheduleInvalidStateError):
        require_occurrence_transition(terminal, OccurrenceState.CLAIMED)
