"""Async Valkey connectivity foundation (NXS-CACHE-001).

A thin internal abstraction over an async Redis-protocol client (Valkey speaks the same
wire protocol). Bounded connect and operation timeouts, a health probe, lifecycle
management and redacted URLs. Domain cache semantics are out of scope for P01.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nexus_ai.core.config import CacheSettings
from nexus_ai.core.errors import ConfigurationError
from nexus_ai.core.health import DependencyHealth, HealthStatus, timed_probe

if TYPE_CHECKING:
    from redis.asyncio import Redis


class Cache:
    def __init__(self, settings: CacheSettings) -> None:
        self._settings = settings
        self._client: Redis | None = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    @property
    def client(self) -> Redis:
        if self._client is None:
            raise ConfigurationError("cache client is not initialised")
        return self._client

    async def connect(self) -> None:
        if self._client is not None:
            return
        if self._settings.url is None:
            raise ConfigurationError("NXS_CACHE__URL is not configured")
        from redis.asyncio import Redis

        self._client = Redis.from_url(
            self._settings.client_url(),
            socket_connect_timeout=self._settings.connect_timeout_seconds,
            socket_timeout=self._settings.operation_timeout_seconds,
            max_connections=self._settings.pool_max_connections,
            decode_responses=True,
            health_check_interval=30,
        )

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def probe(self, *, timeout: float) -> DependencyHealth:
        if self._client is None:
            return DependencyHealth(
                "valkey", HealthStatus.DOWN, self._settings.required, None, "not_connected"
            )

        async def check() -> None:
            await self.client.ping()

        return await timed_probe("valkey", self._settings.required, check, timeout=timeout)
