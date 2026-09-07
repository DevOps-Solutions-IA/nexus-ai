"""Durable consumer idempotency (NXS-EVENT-006).

A PostgreSQL-backed receipt keyed by ``(consumer_name, event_id)``. The claim, the
handler's business effect and the "processed" mark all happen in ONE database
transaction that commits before the NATS acknowledgement — so a lost acknowledgement,
a redelivery or two concurrent deliveries all resolve to exactly one business effect.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai.domain.events.models import ConsumerReceiptRecord
from nexus_ai.events.envelope import EventEnvelope

_MAX_CLAIM_RETRIES = 5


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass(frozen=True, slots=True)
class ReceiptClaim:
    should_process: bool
    already_finished: bool
    attempt: int


class ConsumerReceiptStore:
    async def claim(
        self,
        session: AsyncSession,
        *,
        consumer_name: str,
        envelope: EventEnvelope,
        delivery_count: int,
    ) -> ReceiptClaim:
        """Claim the event for this consumer inside ``session``'s transaction.

        Returns ``should_process=True`` when the caller must run the handler,
        ``already_finished=True`` when a previous delivery already completed (or was
        dead-lettered) and the message should just be acknowledged.
        """
        attempt = max(1, delivery_count)
        for _ in range(_MAX_CLAIM_RETRIES):
            inserted = (
                await session.execute(
                    pg_insert(ConsumerReceiptRecord)
                    .values(
                        consumer_name=consumer_name,
                        event_id=envelope.event_id,
                        organization_id=envelope.organization_id,
                        event_type=envelope.event_type,
                        status="PROCESSING",
                        attempt_count=attempt,
                    )
                    .on_conflict_do_nothing(index_elements=["consumer_name", "event_id"])
                    .returning(ConsumerReceiptRecord.event_id)
                )
            ).scalar_one_or_none()
            if inserted is not None:
                return ReceiptClaim(should_process=True, already_finished=False, attempt=attempt)

            row = (
                await session.execute(
                    select(ConsumerReceiptRecord)
                    .where(
                        ConsumerReceiptRecord.consumer_name == consumer_name,
                        ConsumerReceiptRecord.event_id == envelope.event_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                # The concurrent inserter rolled back before committing — retry.
                continue
            if row.status in {"PROCESSED", "DEAD"}:
                return ReceiptClaim(
                    should_process=False, already_finished=True, attempt=row.attempt_count
                )
            # PROCESSING left by a crashed worker, or FAILED after a retryable error —
            # this delivery takes over.
            next_attempt = row.attempt_count + 1
            await session.execute(
                update(ConsumerReceiptRecord)
                .where(
                    ConsumerReceiptRecord.consumer_name == consumer_name,
                    ConsumerReceiptRecord.event_id == envelope.event_id,
                )
                .values(status="PROCESSING", attempt_count=next_attempt, updated_at=_utcnow())
            )
            return ReceiptClaim(should_process=True, already_finished=False, attempt=next_attempt)
        raise RuntimeError("could not claim a consumer receipt after repeated contention")

    async def mark_processed(
        self, session: AsyncSession, *, consumer_name: str, event_id: object
    ) -> None:
        now = _utcnow()
        await session.execute(
            update(ConsumerReceiptRecord)
            .where(
                ConsumerReceiptRecord.consumer_name == consumer_name,
                ConsumerReceiptRecord.event_id == event_id,
            )
            .values(status="PROCESSED", processed_at=now, last_error_code=None, updated_at=now)
        )

    async def record_terminal(
        self,
        session: AsyncSession,
        *,
        consumer_name: str,
        envelope: EventEnvelope,
        error_code: str,
        attempt: int,
    ) -> None:
        """Upsert a DEAD receipt so a redelivery of this poison event is acknowledged
        immediately instead of re-running the handler."""
        now = _utcnow()
        await session.execute(
            pg_insert(ConsumerReceiptRecord)
            .values(
                consumer_name=consumer_name,
                event_id=envelope.event_id,
                organization_id=envelope.organization_id,
                event_type=envelope.event_type,
                status="DEAD",
                attempt_count=max(1, attempt),
                last_error_code=error_code[:64],
            )
            .on_conflict_do_update(
                index_elements=["consumer_name", "event_id"],
                set_={"status": "DEAD", "last_error_code": error_code[:64], "updated_at": now},
            )
        )
