"""Bounded authentication abuse-control seam (NXS-AUTH-009).

The login path consults a ``LoginRateLimiter`` keyed by a server-computed digest of
(normalized email, client IP) — never by raw client-controlled strings, so limiter keys
carry no personal data. Two backends exist:

* ``CacheRateLimiter`` — a fixed-window counter in the shared Valkey cache. Persistent
  across processes, which is the property a production brute-force defense needs.
* ``LocalWindowRateLimiter`` — an in-process fixed-window counter with a hard cap on
  tracked keys. Bounded memory by construction; it is the deterministic test backend
  and the safe degraded-mode fallback when the cache is unavailable.

The selector is explicit and configuration-validated: hardened environments refuse a
local-only backend, and any runtime cache failure degrades to the bounded local
counter (availability over silence) with a structured warning — never to an unbounded
structure and never to a crash.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections import OrderedDict
from collections.abc import Callable
from typing import Protocol

from nexus_ai.core.config import AuthSettings
from nexus_ai.core.errors import RateLimitedError
from nexus_ai.core.logging import get_logger
from nexus_ai.infrastructure.cache import Cache

_CacheNow = Callable[[], dt.datetime]


class LoginRateLimiter(Protocol):
    async def check(self, key: str) -> None: ...
    async def record_failure(self, key: str) -> None: ...
    async def reset(self, key: str) -> None: ...


def login_rate_key(email: str, client_ip: str) -> str:
    """Digest the identity pair so limiter keys never carry raw email addresses."""
    return hashlib.sha256(f"{email}|{client_ip}".encode()).hexdigest()


class LocalWindowRateLimiter:
    """Fixed-window counter with a hard cap on tracked keys (bounded memory)."""

    def __init__(
        self,
        *,
        max_failures: int,
        window_seconds: int,
        now: _CacheNow | None = None,
        max_keys: int = 100_000,
    ) -> None:
        self._max_failures = max_failures
        self._window = window_seconds
        self._now = now or (lambda: dt.datetime.now(dt.UTC))
        self._max_keys = max_keys
        self._counts: OrderedDict[tuple[str, int], int] = OrderedDict()

    def _window_start(self) -> int:
        return int(self._now().timestamp()) // self._window * self._window

    def _prune(self, window: int) -> None:
        while self._counts and next(reversed(self._counts))[1] < window:
            self._counts.popitem(last=True)

    async def check(self, key: str) -> None:
        window = self._window_start()
        self._prune(window)
        count = self._counts.get((key, window), 0)
        if count >= self._max_failures:
            raise RateLimitedError(
                "Too many failed authentication attempts. Try again later.",
                extensions={"retry_after_seconds": self._window},
            )

    async def record_failure(self, key: str) -> None:
        window = self._window_start()
        self._prune(window)
        entry = (key, window)
        self._counts[entry] = self._counts.get(entry, 0) + 1
        self._counts.move_to_end(entry)
        while len(self._counts) > self._max_keys:
            self._counts.popitem(last=True)

    async def reset(self, key: str) -> None:
        window = self._window_start()
        self._prune(window)
        self._counts.pop((key, window), None)


class CacheRateLimiter:
    """Fixed-window counter stored in the shared Valkey cache (cross-process)."""

    def __init__(
        self,
        cache: Cache,
        *,
        max_failures: int,
        window_seconds: int,
        now: _CacheNow | None = None,
    ) -> None:
        self._cache = cache
        self._max_failures = max_failures
        self._window = window_seconds
        self._now = now or (lambda: dt.datetime.now(dt.UTC))

    def _store_key(self, key: str) -> str:
        window = int(self._now().timestamp()) // self._window
        return f"nxs:auth:login:{key}:{window}"

    async def check(self, key: str) -> None:
        client = self._cache.client
        store_key = self._store_key(key)
        current = await client.get(store_key)
        if current is not None and int(current) >= self._max_failures:
            raise RateLimitedError(
                "Too many failed authentication attempts. Try again later.",
                extensions={"retry_after_seconds": self._window},
            )

    async def record_failure(self, key: str) -> None:
        client = self._cache.client
        store_key = self._store_key(key)
        count = await client.incr(store_key)
        if count == 1:
            await client.expire(store_key, self._window + 1)

    async def reset(self, key: str) -> None:
        await self._cache.client.delete(self._store_key(key))


class RateLimitGate:
    """Backend selection with safe degraded fallback (NXS-AUTH-009).

    ``auto`` prefers the cache and falls back to the bounded local counter when the
    cache is unavailable. ``cache`` requires the cache at startup but still degrades to
    the local counter on runtime failures so authentication never becomes unusable or
    unbounded. ``local`` is local/test only — the settings validator rejects it in
    hardened environments.
    """

    def __init__(
        self,
        settings: AuthSettings,
        cache: Cache,
        *,
        now: _CacheNow | None = None,
    ) -> None:
        self._logger = get_logger("nexus_ai.auth.ratelimit")
        self._local = LocalWindowRateLimiter(
            max_failures=settings.login_max_failures,
            window_seconds=settings.login_failure_window_seconds,
            now=now,
        )
        self._cache_backend: CacheRateLimiter | None = None
        if settings.rate_limit_backend != "local" and cache.is_connected:
            self._cache_backend = CacheRateLimiter(
                cache,
                max_failures=settings.login_max_failures,
                window_seconds=settings.login_failure_window_seconds,
                now=now,
            )

    def _prefer_cache(self) -> bool:
        return self._cache_backend is not None

    async def _with_fallback(self, action: str, key: str) -> None:
        backend: LoginRateLimiter = self._local
        cache_backend = self._cache_backend
        if cache_backend is not None:
            try:
                if action == "check":
                    await cache_backend.check(key)
                    return
                if action == "failure":
                    await cache_backend.record_failure(key)
                    return
                await cache_backend.reset(key)
                return
            except RateLimitedError:
                raise
            except Exception as exc:
                await self._logger.awarning(
                    "auth_rate_limit_backend_failed",
                    backend="cache",
                    error_type=type(exc).__name__,
                )
                backend = self._local
        if action == "check":
            await backend.check(key)
        elif action == "failure":
            await backend.record_failure(key)
        else:
            await backend.reset(key)

    async def check(self, key: str) -> None:
        await self._with_fallback("check", key)

    async def record_failure(self, key: str) -> None:
        await self._with_fallback("failure", key)

    async def reset(self, key: str) -> None:
        await self._with_fallback("reset", key)
