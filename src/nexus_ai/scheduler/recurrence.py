"""Bounded, deterministic recurrence and IANA timezone calculations."""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from zoneinfo import ZoneInfo

from nexus_ai.scheduler.entities import RecurrenceFrequency, RecurrenceSpec
from nexus_ai.scheduler.errors import ScheduleInvalidRecurrenceError

MAX_SEARCH_STEPS = 525_600


def timezone_data_version() -> str:
    try:
        return version("tzdata")
    except PackageNotFoundError:
        return "system"


@dataclass(frozen=True, slots=True)
class TemporalSlot:
    intended_local_time: dt.datetime
    scheduled_for: dt.datetime
    utc_offset_seconds: int
    fold: int
    nonexistent: bool = False


def _resolve_local(local: dt.datetime, zone: ZoneInfo) -> TemporalSlot:
    aware = local.replace(tzinfo=zone, fold=0)
    utc = aware.astimezone(dt.UTC)
    round_trip = utc.astimezone(zone).replace(tzinfo=None)
    nonexistent = round_trip != local
    offset = aware.utcoffset() or dt.timedelta()
    return TemporalSlot(local, utc, int(offset.total_seconds()), 0, nonexistent)


def first_slot(
    *, start_at: dt.datetime, timezone: str, recurrence: RecurrenceSpec | None
) -> TemporalSlot:
    if recurrence is None:
        local = start_at.astimezone(ZoneInfo(timezone)).replace(tzinfo=None)
        return _resolve_local(local, ZoneInfo(timezone))
    anchor = start_at.astimezone(ZoneInfo(timezone)).replace(tzinfo=None, second=0, microsecond=0)
    if recurrence.frequency in {
        RecurrenceFrequency.DAILY,
        RecurrenceFrequency.WEEKLY,
        RecurrenceFrequency.MONTHLY,
    }:
        if recurrence.local_time is None:
            raise ScheduleInvalidRecurrenceError("calendar recurrence requires local_time")
        anchor = dt.datetime.combine(anchor.date(), recurrence.local_time)
        selector_matches = (
            recurrence.frequency is RecurrenceFrequency.DAILY
            or (
                recurrence.frequency is RecurrenceFrequency.WEEKLY
                and anchor.weekday() in recurrence.weekdays
            )
            or (
                recurrence.frequency is RecurrenceFrequency.MONTHLY
                and anchor.day in recurrence.month_days
            )
        )
        if (
            not selector_matches
            or _resolve_local(anchor, ZoneInfo(timezone)).scheduled_for < start_at
        ):
            return next_slot(anchor, start_at, timezone, recurrence)
    slot = _resolve_local(anchor, ZoneInfo(timezone))
    if slot.scheduled_for < start_at:
        return next_slot(anchor, start_at, timezone, recurrence)
    return slot


def next_slot(
    previous_local: dt.datetime,
    after_utc: dt.datetime,
    timezone: str,
    recurrence: RecurrenceSpec,
) -> TemporalSlot:
    zone = ZoneInfo(timezone)
    candidate = previous_local
    for _ in range(MAX_SEARCH_STEPS):
        candidate = _advance(candidate, recurrence)
        slot = _resolve_local(candidate, zone)
        if slot.scheduled_for > after_utc:
            return slot
    raise ScheduleInvalidRecurrenceError("recurrence search exceeded the bounded horizon")


def _advance(value: dt.datetime, recurrence: RecurrenceSpec) -> dt.datetime:
    frequency = recurrence.frequency
    if frequency is RecurrenceFrequency.MINUTELY:
        return value + dt.timedelta(minutes=recurrence.interval)
    if frequency is RecurrenceFrequency.HOURLY:
        return value + dt.timedelta(hours=recurrence.interval)
    if frequency is RecurrenceFrequency.DAILY:
        return value + dt.timedelta(days=recurrence.interval)
    if frequency is RecurrenceFrequency.WEEKLY:
        allowed = set(recurrence.weekdays)
        candidate = value
        anchor_week = value.date() - dt.timedelta(days=value.weekday())
        for _ in range(366 * 5):
            candidate += dt.timedelta(days=1)
            week = candidate.date() - dt.timedelta(days=candidate.weekday())
            week_index = (week - anchor_week).days // 7
            if candidate.weekday() in allowed and week_index % recurrence.interval == 0:
                return candidate
        raise ScheduleInvalidRecurrenceError("weekly recurrence exceeded the bounded horizon")
    month_index = value.year * 12 + value.month - 1
    allowed_days = sorted(recurrence.month_days)
    for offset in range(0, 12 * 5 + 1):
        index = month_index + offset
        year, month_zero = divmod(index, 12)
        month = month_zero + 1
        if offset and offset % recurrence.interval != 0:
            continue
        last_day = calendar.monthrange(year, month)[1]
        for day in allowed_days:
            if day <= last_day:
                candidate = value.replace(year=year, month=month, day=day)
                if candidate > value:
                    return candidate
    raise ScheduleInvalidRecurrenceError("monthly recurrence exceeded the bounded horizon")
