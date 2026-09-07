"""JetStream transport for durable business events (NXS-EVENT-004).

Extends the P01 :class:`~nexus_ai.infrastructure.messaging.Messaging` boundary; it does
NOT open its own NATS connections. Every publish acknowledgement is checked under a
bounded timeout, ``event_id`` is the JetStream message identity for broker-side
deduplication, and there is no silent fallback from durable JetStream to core NATS —
in a hardened environment a missing stream topology fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nexus_ai.core.config import EventsSettings
from nexus_ai.core.logging import get_logger
from nexus_ai.events.errors import DurableTransportUnavailableError, EventPublishError
from nexus_ai.infrastructure.messaging import Messaging

if TYPE_CHECKING:
    from nats.aio.msg import Msg


@dataclass(frozen=True, slots=True)
class PublishReceipt:
    stream: str
    sequence: int
    duplicate: bool


class JetStreamTransport:
    def __init__(
        self,
        messaging: Messaging,
        settings: EventsSettings,
        *,
        environment: str,
    ) -> None:
        self._messaging = messaging
        self._settings = settings
        self._environment = environment
        self._log = get_logger("nexus_ai.events.jetstream")

    @property
    def stream_name(self) -> str:
        return self._settings.stream_name

    @property
    def dead_letter_stream_name(self) -> str:
        return self._settings.dead_letter_stream_name

    def _prefix(self) -> str:
        return self._settings.subject_prefix

    def main_subjects(self) -> list[str]:
        return [
            f"{self._prefix()}.{self._environment}.tenant.>",
            f"{self._prefix()}.{self._environment}.global.>",
        ]

    def tenant_subject_filter(self) -> str:
        return f"{self._prefix()}.{self._environment}.tenant.>"

    def global_subject_filter(self) -> str:
        return f"{self._prefix()}.{self._environment}.global.>"

    def dead_letter_subject_filter(self) -> str:
        return f"{self._prefix()}.{self._environment}.dlq.>"

    def dead_letter_subject(self, *, scope: str, domain: str) -> str:
        return f"{self._prefix()}.{self._environment}.dlq.{scope}.{domain}"

    def durable_available(self) -> bool:
        return self._messaging.is_connected and self._messaging.jetstream_enabled is True

    def _js(self) -> Any:
        if not self.durable_available():
            raise DurableTransportUnavailableError(
                "JetStream is required for durable business events but is not available"
            )
        return self._messaging.jetstream()

    async def ensure_topology(self) -> None:
        """Create or reconcile the required streams. Idempotent."""
        from nats.js.api import DiscardPolicy, RetentionPolicy, StreamConfig

        js = self._js()
        main = StreamConfig(
            name=self._settings.stream_name,
            description="Nexus AI canonical business events (NXS-P04)",
            subjects=self.main_subjects(),
            retention=RetentionPolicy.LIMITS,
            discard=DiscardPolicy.OLD,
            max_age=float(self._settings.stream_max_age_seconds),
            num_replicas=self._settings.stream_replicas,
            duplicate_window=float(self._settings.dedupe_window_seconds),
        )
        dlq = StreamConfig(
            name=self._settings.dead_letter_stream_name,
            description="Nexus AI event dead-letter stream (NXS-P04)",
            subjects=[self.dead_letter_subject_filter()],
            retention=RetentionPolicy.LIMITS,
            discard=DiscardPolicy.OLD,
            max_age=float(self._settings.stream_max_age_seconds),
            num_replicas=self._settings.stream_replicas,
        )
        for config in (main, dlq):
            try:
                await js.add_stream(config=config)
            except Exception:
                await js.update_stream(config=config)
        await self._log.ainfo(
            "jetstream_topology_ready",
            stream=self._settings.stream_name,
            dead_letter_stream=self._settings.dead_letter_stream_name,
        )

    async def topology_ok(self) -> bool:
        """True when the required streams exist with the expected subject filter."""
        if not self.durable_available():
            return False
        js: Any = self._messaging.jetstream()
        try:
            info = await js.stream_info(self._settings.stream_name)
        except Exception:
            return False
        configured = set(info.config.subjects or [])
        return all(subject in configured for subject in self.main_subjects())

    async def publish(
        self,
        subject: str,
        payload: bytes,
        *,
        headers: dict[str, str],
        timeout: float | None = None,
    ) -> PublishReceipt:
        """Publish and CHECK the acknowledgement. Raises :class:`EventPublishError` on any
        transient failure so the caller retries under bounded backoff."""
        js = self._js()
        deadline = timeout or self._settings.publish_timeout_seconds
        try:
            ack = await js.publish(subject, payload, timeout=deadline, headers=headers)
        except DurableTransportUnavailableError:
            raise
        except Exception as exc:
            raise EventPublishError(
                "JetStream did not acknowledge the publication",
                extensions={"error_code": type(exc).__name__[:64]},
            ) from exc
        if not ack.stream:
            raise EventPublishError("JetStream returned an empty acknowledgement")
        return PublishReceipt(
            stream=ack.stream, sequence=int(ack.seq), duplicate=bool(ack.duplicate)
        )

    async def pull_subscribe(self, subject: str, *, durable: str, config: Any) -> Any:
        js = self._js()
        return await js.pull_subscribe(subject, durable=durable, config=config)


def message_delivery_count(msg: Msg) -> int:
    try:
        return int(msg.metadata.num_delivered)
    except Exception:
        return 1
