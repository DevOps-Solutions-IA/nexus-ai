"""The event platform composition root (NXS-P04).

Wires the JetStream transport, the transactional-outbox publisher and relay, the durable
consumer framework and the dead-letter path into one object the application lifespan
starts and drains. It also exposes an end-to-end self-verification probe used by tests
and readiness.
"""

from __future__ import annotations

import uuid

from nexus_ai.core.config import Settings
from nexus_ai.core.errors import ConfigurationError
from nexus_ai.core.health import DependencyHealth, HealthStatus
from nexus_ai.core.logging import get_logger
from nexus_ai.events.consumer import ConsumerGroup, ConsumerSpec, DurableConsumer
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.idempotency import ConsumerReceiptStore
from nexus_ai.events.publisher import EventPublisherService, OutboxRelay
from nexus_ai.events.registry import EVENT_REGISTRY, EventRegistry
from nexus_ai.infrastructure.database import Database
from nexus_ai.infrastructure.event_dead_letter import DeadLetterRepository
from nexus_ai.infrastructure.event_outbox import OutboxRepository
from nexus_ai.infrastructure.jetstream import JetStreamTransport, PublishReceipt
from nexus_ai.infrastructure.messaging import Messaging

_PROBE_SUBJECT_DOMAIN = "platform"


class EventPlatform:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        messaging: Messaging,
        *,
        registry: EventRegistry | None = None,
        worker_name: str | None = None,
    ) -> None:
        self._settings = settings
        self._events = settings.events
        self._database = database
        self._environment = settings.environment.value
        self._registry = registry or EVENT_REGISTRY
        self._log = get_logger("nexus_ai.events.platform")

        self.transport = JetStreamTransport(messaging, self._events, environment=self._environment)
        self.outbox = OutboxRepository()
        self.dead_letters = DeadLetterRepository()
        self.receipts = ConsumerReceiptStore()
        self.publisher = EventPublisherService(
            self.transport, self.outbox, self._events, environment=self._environment
        )
        self.relay = OutboxRelay(
            database,
            self.transport,
            self.outbox,
            self.dead_letters,
            self._events,
            worker_name=worker_name or f"{settings.service_name}-{uuid.uuid4().hex[:8]}",
        )
        self.consumers = ConsumerGroup()
        self._started = False

    # --- lifecycle ---------------------------------------------------------------

    async def start(self) -> None:
        # Fail closed only in a hardened environment: staging/production refuse to start
        # without durable JetStream, exactly like a bad runtime role. Local/test degrade
        # (no relay, no consumers) and stay alive — a transient outage never crashes the
        # process there.
        durable_required = self._settings.is_hardened_environment and self._events.require_jetstream
        if durable_required and not self.transport.durable_available():
            raise ConfigurationError(
                "durable JetStream transport is required but unavailable; refusing to start"
            )
        if self.transport.durable_available() and self._events.bootstrap_topology:
            try:
                await self.transport.ensure_topology()
            except Exception as exc:
                if durable_required:
                    raise ConfigurationError(
                        "the event stream topology could not be established; refusing to start"
                    ) from None
                await self._log.awarning(
                    "event_topology_bootstrap_skipped", error_code=type(exc).__name__[:64]
                )

        if self._events.publisher_enabled and self.transport.durable_available():
            await self.relay.start()
        if self._events.consumers_enabled and self.transport.durable_available():
            await self.consumers.start()
        self._started = True
        await self._log.ainfo(
            "event_platform_started",
            durable=str(self.transport.durable_available()),
            publisher=str(self._events.publisher_enabled),
            consumers=str(len(self.consumers.consumers)),
        )

    async def stop(self) -> None:
        if not self._started:
            return
        await self.consumers.stop()
        await self.relay.stop()
        self._started = False
        await self._log.ainfo("event_platform_stopped")

    # --- consumer registration (future phases / tests) -------------------------

    def register_consumer(self, spec: ConsumerSpec) -> DurableConsumer:
        consumer = DurableConsumer(
            spec,
            database=self._database,
            transport=self.transport,
            dead_letters=self.dead_letters,
            receipts=self.receipts,
            registry=self._registry,
            settings=self._events,
        )
        self.consumers.add(consumer)
        return consumer

    # --- health ----------------------------------------------------------------

    async def probe(self, *, timeout: float) -> DependencyHealth:
        required = self._settings.messaging.required and self._events.require_jetstream
        if not self._events.require_jetstream:
            return DependencyHealth("event_platform", HealthStatus.UP, required=False)
        if not self.transport.durable_available():
            return DependencyHealth(
                "event_platform", HealthStatus.DOWN, required, None, "jetstream_unavailable"
            )
        try:
            ok = await self.transport.topology_ok()
        except Exception:
            ok = False
        status = HealthStatus.UP if ok else HealthStatus.DOWN
        code = None if ok else "stream_topology_missing"
        return DependencyHealth("event_platform", status, required, None, code)

    # --- self-verification ----------------------------------------------------

    async def emit_probe(self, *, note: str | None = None) -> PublishReceipt:
        """Publish a global ``platform.probe.emitted`` event through the confirmed path."""
        envelope = EventEnvelope.create(
            event_type="platform.probe.emitted",
            event_version=1,
            aggregate_type=_PROBE_SUBJECT_DOMAIN,
            aggregate_id=uuid.uuid4().hex,
            producer=self._settings.service_name,
            payload={"nonce": uuid.uuid4().hex, "note": note},
        )
        return await self.publisher.publish_global(envelope)
