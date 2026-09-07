"""Transactional outbox persistence (NXS-EVENT-003, NXS-EVENT-008).

``enqueue`` runs inside the caller's tenant transaction, so the outbox row and the
business mutation commit atomically and forced Row-Level Security (``WITH CHECK``)
guarantees the row's ``organization_id`` equals the bound tenant scope — a forged tenant
in an event payload can never poison the outbox.

``claim_batch`` runs in an explicitly UNSCOPED system transaction (the relay policy).
It uses ``SELECT ... FOR UPDATE SKIP LOCKED`` so many relay workers never publish the
same row concurrently, and it also reclaims rows whose ``PUBLISHING`` lease has expired
because a worker crashed — no row is ever permanently stuck.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai.domain.events.models import EventOutboxRecord
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.errors import EventTenantScopeError


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass(frozen=True, slots=True)
class OutboxItem:
    id: uuid.UUID
    organization_id: uuid.UUID
    event_type: str
    event_version: int
    subject: str
    envelope: dict[str, object]
    attempt_count: int

    def to_envelope(self) -> EventEnvelope:
        return EventEnvelope.model_validate(self.envelope)


class OutboxRepository:
    async def enqueue(self, session: AsyncSession, envelope: EventEnvelope, subject: str) -> bool:
        """Insert an outbox row in the caller's (tenant) transaction. Idempotent on
        ``event_id``. Returns ``True`` when a new row was written."""
        organization_id = envelope.require_tenant()
        record = EventOutboxRecord(
            id=envelope.event_id,
            organization_id=organization_id,
            event_type=envelope.event_type,
            event_version=envelope.event_version,
            subject=subject,
            envelope=envelope.model_dump(mode="json"),
            status="PENDING",
            attempt_count=0,
            available_at=_utcnow(),
        )
        # A SAVEPOINT so a duplicate-enqueue conflict or an RLS rejection never rolls
        # back the caller's business mutation.
        try:
            async with session.begin_nested():
                session.add(record)
                await session.flush()
        except IntegrityError as exc:
            if _is_primary_key_conflict(exc):
                return False
            raise
        except DBAPIError as exc:
            if _is_rls_violation(exc):
                raise EventTenantScopeError(
                    "the event organization_id does not match the bound tenant scope"
                ) from exc
            raise
        return True

    async def claim_batch(
        self,
        session: AsyncSession,
        *,
        owner: str,
        batch_size: int,
        lease_seconds: int,
    ) -> list[OutboxItem]:
        """Claim up to ``batch_size`` publishable rows. UNSCOPED system transaction."""
        now = _utcnow()
        lease_until = now + dt.timedelta(seconds=lease_seconds)
        candidate = (
            select(EventOutboxRecord.id)
            .where(
                text(
                    "(status IN ('PENDING', 'FAILED') AND available_at <= :now) "
                    "OR (status = 'PUBLISHING' AND lease_expires_at <= :now)"
                )
            )
            .order_by(EventOutboxRecord.available_at.asc())
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        ids = list((await session.execute(candidate, {"now": now})).scalars().all())
        if not ids:
            return []
        claimed = (
            (
                await session.execute(
                    update(EventOutboxRecord)
                    .where(EventOutboxRecord.id.in_(ids))
                    .values(
                        status="PUBLISHING",
                        lease_owner=owner,
                        lease_expires_at=lease_until,
                        attempt_count=EventOutboxRecord.attempt_count + 1,
                        updated_at=now,
                    )
                    .returning(EventOutboxRecord)
                )
            )
            .scalars()
            .all()
        )
        return [
            OutboxItem(
                id=row.id,
                organization_id=row.organization_id,
                event_type=row.event_type,
                event_version=row.event_version,
                subject=row.subject,
                envelope=dict(row.envelope),
                attempt_count=row.attempt_count,
            )
            for row in claimed
        ]

    async def mark_published(self, session: AsyncSession, outbox_id: uuid.UUID) -> None:
        now = _utcnow()
        await session.execute(
            update(EventOutboxRecord)
            .where(EventOutboxRecord.id == outbox_id)
            .values(
                status="PUBLISHED",
                published_at=now,
                lease_owner=None,
                lease_expires_at=None,
                last_error_code=None,
                updated_at=now,
            )
        )

    async def reschedule(
        self,
        session: AsyncSession,
        outbox_id: uuid.UUID,
        *,
        error_code: str,
        available_at: dt.datetime,
    ) -> None:
        await session.execute(
            update(EventOutboxRecord)
            .where(EventOutboxRecord.id == outbox_id)
            .values(
                status="FAILED",
                last_error_code=error_code[:64],
                available_at=available_at,
                lease_owner=None,
                lease_expires_at=None,
                updated_at=_utcnow(),
            )
        )

    async def mark_dead(
        self, session: AsyncSession, outbox_id: uuid.UUID, *, error_code: str
    ) -> None:
        await session.execute(
            update(EventOutboxRecord)
            .where(EventOutboxRecord.id == outbox_id)
            .values(
                status="DEAD",
                last_error_code=error_code[:64],
                lease_owner=None,
                lease_expires_at=None,
                updated_at=_utcnow(),
            )
        )

    async def pending_count(self, session: AsyncSession) -> int:
        return int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM event_outbox "
                        "WHERE status IN ('PENDING', 'PUBLISHING', 'FAILED')"
                    )
                )
            ).scalar_one()
        )


def _is_primary_key_conflict(exc: IntegrityError) -> bool:
    message = str(exc.orig) if exc.orig is not None else str(exc)
    return "pk_event_outbox" in message or "duplicate key" in message


def _is_rls_violation(exc: DBAPIError) -> bool:
    message = str(exc.orig) if exc.orig is not None else str(exc)
    return "row-level security" in message or "row level security" in message
