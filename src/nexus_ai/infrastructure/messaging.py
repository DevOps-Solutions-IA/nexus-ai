"""NATS / JetStream connectivity foundation (NXS-EVENT-001).

An async connection manager with bounded connect timeout, a reconnect policy, lifecycle
callbacks, JetStream capability detection, health state and graceful drain. Application
subjects and business event contracts are out of scope for P01 (they belong to P04).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from nexus_ai.core.config import MessagingSettings
from nexus_ai.core.errors import ConfigurationError
from nexus_ai.core.health import DependencyHealth, HealthStatus, timed_probe
from nexus_ai.core.logging import get_logger

if TYPE_CHECKING:
    from nats.aio.client import Client as NatsClient


class Messaging:
    def __init__(self, settings: MessagingSettings) -> None:
        self._settings = settings
        self._client: NatsClient | None = None
        self._jetstream_enabled: bool | None = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    @property
    def jetstream_enabled(self) -> bool | None:
        return self._jetstream_enabled

    @property
    def client(self) -> NatsClient:
        if self._client is None:
            raise ConfigurationError("messaging client is not initialised")
        return self._client

    def jetstream(self) -> object:
        """The JetStream context for durable publishing/consuming (NXS-EVENT-004).

        P04 owns application subjects and business event contracts; it builds on this
        boundary rather than opening its own NATS clients.
        """
        if self._client is None:
            raise ConfigurationError("messaging client is not initialised")
        return self._client.jetstream()

    async def connect(self) -> None:
        if self._client is not None:
            return
        if self._settings.url is None:
            raise ConfigurationError("NXS_MESSAGING__URL is not configured")
        import nats

        logger = get_logger("nexus_ai.messaging")

        async def _disconnected() -> None:
            await logger.awarning("nats_disconnected")

        async def _reconnected() -> None:
            await logger.ainfo("nats_reconnected")

        async def _error(exc: Exception) -> None:
            await logger.awarning("nats_error", error_code=type(exc).__name__)

        async def _closed() -> None:
            await logger.ainfo("nats_connection_closed")

        deadline = self._settings.connect_timeout_seconds + 2.0
        self._client = await asyncio.wait_for(
            nats.connect(
                servers=self._settings.servers(),
                connect_timeout=self._settings.connect_timeout_seconds,
                reconnect_time_wait=self._settings.reconnect_time_wait_seconds,
                max_reconnect_attempts=self._settings.max_reconnect_attempts,
                allow_reconnect=True,
                disconnected_cb=_disconnected,
                reconnected_cb=_reconnected,
                error_cb=_error,
                closed_cb=_closed,
            ),
            timeout=deadline,
        )
        self._jetstream_enabled = await self._detect_jetstream()

    async def _detect_jetstream(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.jetstream().account_info()
        except Exception:
            return False
        return True

    async def disconnect(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.drain()
        except Exception:
            await self._client.close()
        finally:
            self._client = None
            self._jetstream_enabled = None

    async def probe(self, *, timeout: float) -> DependencyHealth:
        if self._client is None or not self._client.is_connected:
            return DependencyHealth(
                "nats", HealthStatus.DOWN, self._settings.required, None, "not_connected"
            )

        flush_timeout = max(1, round(timeout))

        async def check() -> None:
            await self.client.flush(timeout=flush_timeout)

        health = await timed_probe("nats", self._settings.required, check, timeout=timeout + 1)
        if health.status is HealthStatus.UP and self._jetstream_enabled is False:
            return DependencyHealth(
                "nats",
                HealthStatus.DEGRADED,
                self._settings.required,
                health.latency_ms,
                "jetstream_unavailable",
            )
        return health
