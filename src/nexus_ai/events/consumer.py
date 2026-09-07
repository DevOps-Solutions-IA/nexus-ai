"""Durable consumer framework (NXS-EVENT-005, NXS-EVENT-006, NXS-EVENT-007).

A reusable pull-based JetStream consumer for future phases. Guarantees:

* a message is ACKed only after the handler's effect and the idempotency receipt commit;
* a retryable failure is NAKed with bounded exponential backoff;
* a terminal (poison) failure is recorded, dead-lettered and TERMed — never hot-looped;
* the handler runs inside a database transaction whose tenant scope is bound from the
  TRUSTED envelope (Row-Level Security is the final boundary);
* concurrency, the fetch batch size, the ack-wait and the handler timeout are all bounded
  and configuration-validated;
* ``stop`` drains in-flight work before returning.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

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
    HandlerRetryableError,
    HandlerTerminalError,
    classify_failure,
    safe_error_code,
    safe_error_summary,
)
from nexus_ai.events.idempotency import ConsumerReceiptStore
from nexus_ai.events.registry import EVENT_REGISTRY, EventPayload, EventRegistry
from nexus_ai.events.subjects import parse_subject
from nexus_ai.infrastructure.database import Database
from nexus_ai.infrastructure.event_dead_letter import DeadLetterRepository
from nexus_ai.infrastructure.jetstream import JetStreamTransport, message_delivery_count

if TYPE_CHECKING:
    from nats.aio.msg import Msg


@dataclass(frozen=True, slots=True)
class EventContext:
    envelope: EventEnvelope
    payload: EventPayload
    session: AsyncSession
    attempt: int


EventHandler = Callable[[EventContext], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ConsumerSpec:
    name: str
    subject_filter: str
    handler: EventHandler
    event_types: frozenset[str]
    max_delivery_attempts: int | None = None


@dataclass(slots=True)
class ConsumerStats:
    processed: int = 0
    duplicates: int = 0
    retried: int = 0
    dead_lettered: int = 0
    errors: int = 0


class DurableConsumer:
    def __init__(
        self,
        spec: ConsumerSpec,
        *,
        database: Database,
        transport: JetStreamTransport,
        dead_letters: DeadLetterRepository,
        receipts: ConsumerReceiptStore | None = None,
        registry: EventRegistry | None = None,
        settings: EventsSettings,
    ) -> None:
        self._spec = spec
        self._db = database
        self._transport = transport
        self._dead_letters = dead_letters
        self._receipts = receipts or ConsumerReceiptStore()
        self._registry = registry or EVENT_REGISTRY
        self._settings = settings
        self._log = get_logger("nexus_ai.events.consumer").bind(consumer=spec.name)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._subscription: object | None = None
        self._semaphore = asyncio.Semaphore(settings.consumer_concurrency)
        self.stats = ConsumerStats()

    @property
    def max_attempts(self) -> int:
        return self._spec.max_delivery_attempts or self._settings.max_delivery_attempts

    # --- lifecycle ---------------------------------------------------------------

    async def ensure_subscription(self) -> object:
        if self._subscription is not None:
            return self._subscription
        from nats.js.api import AckPolicy, ConsumerConfig

        config = ConsumerConfig(
            durable_name=self._spec.name,
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=self._settings.consumer_ack_wait_seconds,
            max_deliver=self.max_attempts,
            max_ack_pending=self._settings.consumer_concurrency * 4,
            filter_subject=self._spec.subject_filter,
        )
        self._subscription = await self._transport.pull_subscribe(
            self._spec.subject_filter, durable=self._spec.name, config=config
        )
        return self._subscription

    async def start(self) -> None:
        await self.ensure_subscription()
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name=f"consumer-{self._spec.name}")
        await self._log.ainfo("consumer_started", subject_filter=self._spec.subject_filter)

    async def run_pending(self, *, max_messages: int | None = None, timeout: float = 3.0) -> int:
        """Fetch and process one batch synchronously. Used by drains and tests."""
        subscription = await self.ensure_subscription()
        batch = max_messages or self._settings.consumer_batch_size
        try:
            messages = await subscription.fetch(batch, timeout=timeout)  # type: ignore[attr-defined]
        except TimeoutError:
            return 0
        await asyncio.gather(*(self._guarded(message) for message in messages))
        return len(messages)

    async def drain_pending(self, *, max_cycles: int = 50, timeout: float = 2.0) -> int:
        total = 0
        for _ in range(max_cycles):
            handled = await self.run_pending(timeout=timeout)
            total += handled
            if handled == 0:
                break
        return total

    async def stop(self, *, timeout: float = 20.0) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout)
            except TimeoutError:
                self._task.cancel()
            self._task = None
        sub = self._subscription
        if sub is not None:
            with contextlib.suppress(Exception):
                await sub.unsubscribe()  # type: ignore[attr-defined]
            self._subscription = None
        await self._log.ainfo(
            "consumer_stopped", **{k: str(v) for k, v in asdict(self.stats).items()}
        )

    async def _run(self) -> None:
        subscription = self._subscription
        if subscription is None:  # pragma: no cover - start() always sets it first
            raise EventPlatformError("consumer run loop started without a subscription")
        while not self._stop.is_set():
            try:
                messages = await subscription.fetch(  # type: ignore[attr-defined]
                    self._settings.consumer_batch_size,
                    timeout=self._settings.consumer_poll_timeout_seconds,
                )
            except TimeoutError:
                continue
            except Exception as exc:
                await self._log.awarning("consumer_fetch_error", error_code=safe_error_code(exc))
                await asyncio.sleep(self._settings.publisher_poll_interval_seconds)
                continue
            await asyncio.gather(*(self._guarded(message) for message in messages))

    async def _guarded(self, message: Msg) -> None:
        async with self._semaphore:
            try:
                await self._process(message)
            except Exception as exc:
                self.stats.errors += 1
                await self._log.aerror("consumer_unhandled_error", error_code=safe_error_code(exc))
                with contextlib.suppress(Exception):
                    await message.nak(delay=self._settings.retry_base_delay_seconds)

    # --- message processing ----------------------------------------------------

    async def _process(self, message: Msg) -> None:
        try:
            envelope = EventEnvelope.from_json(message.data)
        except EventContractError as exc:
            await self._malformed(message, exc)
            return

        try:
            self._check_provenance(message.subject, envelope)
            payload = self._registry.decode(envelope)
        except EventPlatformError as exc:
            await self._terminal(message, envelope, exc, delivery=message_delivery_count(message))
            return

        if envelope.event_type not in self._spec.event_types:
            await self._terminal(
                message,
                envelope,
                EventContractError(
                    "consumer received an event type it does not declare",
                    extensions={"event_type": envelope.event_type},
                ),
                delivery=message_delivery_count(message),
            )
            return

        delivery = message_delivery_count(message)
        try:
            async with self._open(envelope) as session:
                claim = await self._receipts.claim(
                    session,
                    consumer_name=self._spec.name,
                    envelope=envelope,
                    delivery_count=delivery,
                )
                if claim.already_finished:
                    self.stats.duplicates += 1
                else:
                    await asyncio.wait_for(
                        self._spec.handler(
                            EventContext(
                                envelope=envelope,
                                payload=payload,
                                session=session,
                                attempt=claim.attempt,
                            )
                        ),
                        timeout=self._settings.handler_timeout_seconds,
                    )
                    await self._receipts.mark_processed(
                        session, consumer_name=self._spec.name, event_id=envelope.event_id
                    )
            await message.ack()
            if not claim.already_finished:
                self.stats.processed += 1
                await self._log.ainfo(
                    "event_processed", attempt=str(claim.attempt), **envelope.log_fields()
                )
        except HandlerTerminalError as exc:
            await self._terminal(message, envelope, exc, delivery=delivery)
        except (TimeoutError, HandlerRetryableError) as exc:
            await self._retry_or_terminal(message, envelope, exc, delivery)
        except Exception as exc:
            if classify_failure(exc) is FailureClass.RETRYABLE:
                await self._retry_or_terminal(message, envelope, exc, delivery)
            else:
                await self._terminal(message, envelope, exc, delivery=delivery)

    def _check_provenance(self, raw_subject: str, envelope: EventEnvelope) -> None:
        """The subject and the trusted envelope must agree on scope and domain. Tenant
        authority is the envelope's ``organization_id`` (written server-side under RLS),
        never a payload field."""
        subject = parse_subject(raw_subject)
        if subject.scope.value != envelope.scope.value:
            raise EventTenantScopeError("subject scope does not match the envelope scope")
        if subject.domain != envelope.domain:
            raise EventTenantScopeError("subject domain does not match the envelope event type")
        payload_org = envelope.payload.get("organization_id")
        if payload_org is not None and str(payload_org) != str(envelope.organization_id):
            raise EventTenantScopeError("payload organization_id disagrees with the envelope")

    @asynccontextmanager
    async def _open(self, envelope: EventEnvelope) -> AsyncIterator[AsyncSession]:
        if envelope.scope is EventScope.TENANT:
            async with self._db.tenant_transaction(envelope.require_tenant()) as tenant_session:
                yield tenant_session.session
        else:
            async with self._db.transaction() as session:
                yield session

    # --- failure routing -----------------------------------------------------

    async def _retry_or_terminal(
        self, message: Msg, envelope: EventEnvelope, exc: BaseException, delivery: int
    ) -> None:
        if should_dead_letter(attempt=delivery, max_attempts=self.max_attempts):
            await self._terminal(message, envelope, exc, delivery=delivery)
            return
        delay = backoff_delay(
            attempt=delivery,
            base_seconds=self._settings.retry_base_delay_seconds,
            max_seconds=self._settings.retry_max_delay_seconds,
        )
        with contextlib.suppress(Exception):
            await message.nak(delay=delay)
        self.stats.retried += 1
        await self._log.awarning(
            "event_processing_retry",
            error_code=safe_error_code(exc),
            attempt=str(delivery),
            retry_in_seconds=str(round(delay, 2)),
            **envelope.log_fields(),
        )

    async def _terminal(
        self, message: Msg, envelope: EventEnvelope, exc: BaseException, *, delivery: int
    ) -> None:
        error_code = safe_error_code(exc)
        failure_class = classify_failure(exc)
        try:
            async with self._open(envelope) as session:
                await self._receipts.record_terminal(
                    session,
                    consumer_name=self._spec.name,
                    envelope=envelope,
                    error_code=error_code,
                    attempt=delivery,
                )
                await self._dead_letters.record(
                    session,
                    envelope=envelope,
                    origin="CONSUMER",
                    failure_class=failure_class,
                    error_code=error_code,
                    attempt_count=delivery,
                    consumer_name=self._spec.name,
                    error_summary=safe_error_summary(exc),
                )
        except Exception as store_exc:
            await self._log.aerror(
                "dead_letter_persist_failed", error_code=safe_error_code(store_exc)
            )
            with contextlib.suppress(Exception):
                await message.nak(delay=self._settings.retry_max_delay_seconds)
            return
        await self._publish_dead_letter(envelope, error_code)
        with contextlib.suppress(Exception):
            await message.term()
        self.stats.dead_lettered += 1
        await self._log.aerror(
            "event_dead_lettered",
            origin="CONSUMER",
            error_code=error_code,
            failure_class=failure_class.value,
            attempt=str(delivery),
            **envelope.log_fields(),
        )

    async def _malformed(self, message: Msg, exc: EventContractError) -> None:
        self.stats.dead_lettered += 1
        await self._log.aerror(
            "event_unparseable",
            error_code=safe_error_code(exc),
            subject=message.subject,
            byte_length=str(len(message.data)),
        )
        with contextlib.suppress(Exception):
            await message.term()

    async def _publish_dead_letter(self, envelope: EventEnvelope, error_code: str) -> None:
        subject = self._transport.dead_letter_subject(
            scope=envelope.scope.value, domain=envelope.domain
        )
        headers = envelope.nats_headers()
        headers["Nxs-Dead-Letter-Reason"] = error_code
        with contextlib.suppress(Exception):
            await self._transport.publish(subject, envelope.to_json(), headers=headers)


@dataclass(slots=True)
class ConsumerGroup:
    """A set of durable consumers started and drained together."""

    consumers: list[DurableConsumer] = field(default_factory=list)

    def add(self, consumer: DurableConsumer) -> None:
        self.consumers.append(consumer)

    async def start(self) -> None:
        for consumer in self.consumers:
            await consumer.start()

    async def stop(self, *, timeout: float = 20.0) -> None:
        await asyncio.gather(
            *(consumer.stop(timeout=timeout) for consumer in self.consumers),
            return_exceptions=True,
        )


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
