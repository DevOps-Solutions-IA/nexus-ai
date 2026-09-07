"""Event publication: the transactional outbox relay and the direct global publisher
(NXS-EVENT-003, NXS-EVENT-004).

A tenant business event is ENQUEUED into the outbox inside the same transaction as the
business mutation. The :class:`OutboxRelay` — one or more workers — later claims rows
with ``FOR UPDATE SKIP LOCKED``, publishes each to JetStream, checks the acknowledgement
and only then marks the row ``PUBLISHED``. A committed outbox row therefore survives any
transient NATS outage: it is retried under bounded backoff and, after
``max_publish_attempts``, dead-lettered — never lost, never hot-looped.

Global platform events carry no tenant transaction to be atomic with, so they are
published directly through the same confirmed JetStream path.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai.core.config import EventsSettings
from nexus_ai.core.logging import get_logger
from nexus_ai.events.backoff import backoff_delay, should_dead_letter
from nexus_ai.events.envelope import EventEnvelope, EventScope
from nexus_ai.events.errors import (
    EventContractError,
    EventPlatformError,
    EventTenantScopeError,
    FailureClass,
    classify_failure,
    safe_error_code,
    safe_error_summary,
)
from nexus_ai.infrastructure.database import Database
from nexus_ai.infrastructure.event_dead_letter import DeadLetterRepository
from nexus_ai.infrastructure.event_outbox import OutboxItem, OutboxRepository
from nexus_ai.infrastructure.jetstream import JetStreamTransport, PublishReceipt


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class EventPublisher(Protocol):
    async def enqueue(self, session: AsyncSession, envelope: EventEnvelope) -> bool: ...

    async def publish_global(self, envelope: EventEnvelope) -> PublishReceipt: ...


class EventPublisherService:
    """The application-facing publisher. Domains call ``enqueue`` from inside their
    tenant unit of work; nothing here talks to NATS synchronously on the request path."""

    def __init__(
        self,
        transport: JetStreamTransport,
        outbox: OutboxRepository,
        settings: EventsSettings,
        *,
        environment: str,
    ) -> None:
        self._transport = transport
        self._outbox = outbox
        self._settings = settings
        self._environment = environment

    def subject_for(self, envelope: EventEnvelope) -> str:
        return envelope.subject(
            prefix=self._settings.subject_prefix, environment=self._environment
        ).value

    async def enqueue(self, session: AsyncSession, envelope: EventEnvelope) -> bool:
        """Write a tenant event to the outbox in the caller's transaction."""
        if envelope.scope is not EventScope.TENANT:
            raise EventTenantScopeError("only tenant-scoped events use the outbox")
        return await self._outbox.enqueue(session, envelope, self.subject_for(envelope))

    async def publish_global(self, envelope: EventEnvelope) -> PublishReceipt:
        if envelope.scope is not EventScope.GLOBAL:
            raise EventTenantScopeError("publish_global requires a global-scoped event")
        return await self._transport.publish(
            self.subject_for(envelope),
            envelope.to_json(),
            headers=envelope.nats_headers(),
        )


class OutboxRelay:
    def __init__(
        self,
        database: Database,
        transport: JetStreamTransport,
        outbox: OutboxRepository,
        dead_letters: DeadLetterRepository,
        settings: EventsSettings,
        *,
        worker_name: str,
    ) -> None:
        self._db = database
        self._transport = transport
        self._outbox = outbox
        self._dead_letters = dead_letters
        self._settings = settings
        self._worker = worker_name[:64]
        self._log = get_logger("nexus_ai.events.outbox_relay")
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name=f"outbox-relay-{self._worker}")

    async def stop(self, *, timeout: float = 15.0) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout)
            except TimeoutError:
                self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await self.run_once()
            except Exception as exc:
                await self._log.awarning(
                    "outbox_relay_cycle_error", error_code=safe_error_code(exc)
                )
                processed = 0
            if processed == 0:
                await self._sleep(self._settings.publisher_poll_interval_seconds)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            return

    async def run_once(self) -> int:
        """Claim and publish one batch. Returns the number of rows processed."""
        async with self._db.transaction() as session:
            items = await self._outbox.claim_batch(
                session,
                owner=self._worker,
                batch_size=self._settings.publisher_batch_size,
                lease_seconds=self._settings.publisher_lease_seconds,
            )
        for item in items:
            await self._publish_one(item)
        return len(items)

    async def _publish_one(self, item: OutboxItem) -> None:
        try:
            envelope = item.to_envelope()
        except Exception as exc:
            await self._dead_letter(item, EventContractError("stored envelope is invalid"), exc)
            return
        try:
            receipt = await self._transport.publish(
                item.subject,
                envelope.to_json(),
                headers=envelope.nats_headers(),
                timeout=self._settings.publish_timeout_seconds,
            )
        except EventPlatformError as exc:
            # A transient transport failure (no ack, timeout, JetStream unavailable) is
            # retried under bounded backoff and only dead-lettered after the budget is
            # spent. A terminal contract failure is dead-lettered immediately.
            if classify_failure(exc) is FailureClass.RETRYABLE:
                await self._handle_transient(item, envelope, exc)
            else:
                await self._dead_letter(item, exc, exc, envelope=envelope)
            return
        async with self._db.transaction() as session:
            await self._outbox.mark_published(session, item.id)
        await self._log.ainfo(
            "event_published",
            duplicate=str(receipt.duplicate),
            stream_sequence=str(receipt.sequence),
            attempt=str(item.attempt_count),
            **envelope.log_fields(),
        )

    async def _handle_transient(
        self, item: OutboxItem, envelope: EventEnvelope, exc: EventPlatformError
    ) -> None:
        if should_dead_letter(
            attempt=item.attempt_count, max_attempts=self._settings.max_publish_attempts
        ):
            await self._dead_letter(item, exc, exc, envelope=envelope)
            return
        delay = backoff_delay(
            attempt=item.attempt_count,
            base_seconds=self._settings.retry_base_delay_seconds,
            max_seconds=self._settings.retry_max_delay_seconds,
        )
        async with self._db.transaction() as session:
            await self._outbox.reschedule(
                session,
                item.id,
                error_code=safe_error_code(exc),
                available_at=_utcnow() + dt.timedelta(seconds=delay),
            )
        await self._log.awarning(
            "event_publish_retry_scheduled",
            error_code=safe_error_code(exc),
            attempt=str(item.attempt_count),
            retry_in_seconds=str(round(delay, 2)),
            **envelope.log_fields(),
        )

    async def _dead_letter(
        self,
        item: OutboxItem,
        classified: BaseException,
        cause: BaseException,
        *,
        envelope: EventEnvelope | None = None,
    ) -> None:
        error_code = safe_error_code(cause)
        failure_class = classify_failure(classified)
        async with self._db.transaction() as session:
            await self._outbox.mark_dead(session, item.id, error_code=error_code)
            if envelope is not None:
                await self._dead_letters.record(
                    session,
                    envelope=envelope,
                    origin="OUTBOX_PUBLISH",
                    failure_class=failure_class,
                    error_code=error_code,
                    attempt_count=item.attempt_count,
                    error_summary=safe_error_summary(cause),
                )
        await self._log.aerror(
            "event_dead_lettered",
            origin="OUTBOX_PUBLISH",
            error_code=error_code,
            failure_class=failure_class.value,
            attempt=str(item.attempt_count),
            event_id=str(item.id),
            event_type=item.event_type,
        )

    async def drain_now(self, *, max_batches: int = 100) -> int:
        """Publish every currently-available row. Used by tests and the self-check."""
        total = 0
        for _ in range(max_batches):
            processed = await self.run_once()
            total += processed
            if processed == 0:
                break
        return total
