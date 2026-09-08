"""Bounded outbound rate limiting for the Integration Hub (NXS-INT-001).

Every outbound integration call passes a per-organization (optionally per-integration)
limiter before it reaches the network, so a runaway caller or a hot retry loop cannot
flood a provider from Nexus. This is NOT cost metering or billing (NXS-P23) — it is a
protective ceiling.

The limiter is a fixed-window counter that reuses the P03 cache pattern: a shared Valkey
counter when the cache is connected (cross-process), degrading to a bounded in-process
counter otherwise (availability over silence). An upstream 429 is normalised elsewhere
(:mod:`nexus_ai.integrations.result`); this module governs only Nexus-initiated volume.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from uuid import UUID

from nexus_ai.core.config import IntegrationsSettings
from nexus_ai.core.logging import get_logger
from nexus_ai.infrastructure.cache import Cache
from nexus_ai.integrations.errors import IntegrationOutboundRateLimitedError

_MAX_LOCAL_KEYS = 100_000


class OutboundRateLimiter:
    def __init__(
        self,
        settings: IntegrationsSettings,
        cache: Cache,
        *,
        now: object | None = None,
    ) -> None:
        self._limit = settings.outbound_rate_limit_per_minute
        self._window = 60
        self._cache = cache
        self._now = now or time.time
        self._log = get_logger("nexus_ai.integrations.ratelimit")
        self._local: OrderedDict[tuple[str, int], int] = OrderedDict()

    def _key(self, organization_id: UUID, integration_id: UUID | None) -> str:
        target = "all" if integration_id is None else str(integration_id)
        return f"{organization_id}:{target}"

    def _window_start(self) -> int:
        return int(self._now()) // self._window  # type: ignore[operator]

    async def check_and_consume(
        self, organization_id: UUID, integration_id: UUID | None = None
    ) -> None:
        key = self._key(organization_id, integration_id)
        window = self._window_start()
        count = await self._increment(key, window)
        if count > self._limit:
            raise IntegrationOutboundRateLimitedError(
                "the Integration Hub outbound rate limit was exceeded for this Organization",
                retry_after_seconds=float(self._window),
            )

    async def _increment(self, key: str, window: int) -> int:
        if self._cache.is_connected:
            try:
                store_key = f"nxs:int:outbound:{key}:{window}"
                client = self._cache.client
                count = await client.incr(store_key)
                if count == 1:
                    await client.expire(store_key, self._window + 1)
                return int(count)
            except Exception as exc:
                # Degrade to the bounded local counter — availability over silence.
                await self._log.awarning(
                    "outbound_rate_limit_backend_failed", error_type=type(exc).__name__
                )
        entry = (key, window)
        # Prune stale windows first so the map cannot grow unbounded.
        for stale in [k for k in self._local if k[1] < window][:1024]:
            self._local.pop(stale, None)
        self._local[entry] = self._local.get(entry, 0) + 1
        self._local.move_to_end(entry)
        while len(self._local) > _MAX_LOCAL_KEYS:
            self._local.popitem(last=False)
        return self._local[entry]
