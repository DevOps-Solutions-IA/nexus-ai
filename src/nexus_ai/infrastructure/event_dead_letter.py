"""Dead-letter persistence for terminally failed tenant events (NXS-EVENT-007).

A durable row retains the event identity, the failure classification, the attempt count
and a SANITIZED error summary — never a stack trace or a secret. Replay is a controlled,
audited operation (``scripts.nxs_events replay``), never an automatic infinite retry.
Only tenant-scoped events get a durable row; global terminal failures go to the
dead-letter stream and the structured log.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai.domain.events.models import EventDeadLetterRecord
from nexus_ai.events.envelope import EventEnvelope, EventScope
from nexus_ai.events.errors import FailureClass

_MAX_SUMMARY = 480


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def sanitize_summary(text_value: str | None) -> str | None:
    if not text_value:
        return None
    single_line = " ".join(text_value.split())
    return single_line[:_MAX_SUMMARY]


class DeadLetterRepository:
    async def record(
        self,
        session: AsyncSession,
        *,
        envelope: EventEnvelope,
        origin: str,
        failure_class: FailureClass,
        error_code: str,
        attempt_count: int,
        consumer_name: str | None = None,
        error_summary: str | None = None,
    ) -> bool:
        """Insert a dead-letter row. Idempotent on ``(event_id, origin, consumer_name)``
        via an existence check. Returns ``True`` when a new row was written."""
        if envelope.scope is not EventScope.TENANT or envelope.organization_id is None:
            return False
        exists = (
            await session.execute(
                text(
                    "SELECT 1 FROM event_dead_letters "
                    "WHERE event_id = :event_id AND origin = :origin "
                    "AND consumer_name IS NOT DISTINCT FROM :consumer_name LIMIT 1"
                ),
                {
                    "event_id": envelope.event_id,
                    "origin": origin,
                    "consumer_name": consumer_name,
                },
            )
        ).first()
        if exists is not None:
            return False
        session.add(
            EventDeadLetterRecord(
                id=uuid.uuid7(),
                organization_id=envelope.organization_id,
                event_id=envelope.event_id,
                event_type=envelope.event_type,
                event_version=envelope.event_version,
                origin=origin,
                consumer_name=consumer_name,
                failure_class=failure_class.value,
                error_code=error_code[:64],
                error_summary=sanitize_summary(error_summary),
                attempt_count=attempt_count,
                envelope=envelope.model_dump(mode="json"),
            )
        )
        await session.flush()
        return True

    async def list_for_replay(
        self, session: AsyncSession, *, limit: int = 50
    ) -> list[EventDeadLetterRecord]:
        return list(
            (
                await session.execute(
                    select(EventDeadLetterRecord)
                    .where(EventDeadLetterRecord.replayed_at.is_(None))
                    .order_by(EventDeadLetterRecord.recorded_at.asc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    async def mark_replayed(self, session: AsyncSession, dead_letter_id: uuid.UUID) -> None:
        await session.execute(
            update(EventDeadLetterRecord)
            .where(EventDeadLetterRecord.id == dead_letter_id)
            .values(replayed_at=_utcnow())
        )
