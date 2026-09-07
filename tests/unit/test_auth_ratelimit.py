"""Authentication abuse-control seam (NXS-AUTH-009)."""

from __future__ import annotations

import datetime as dt

import pytest

from nexus_ai.core.config import AuthSettings
from nexus_ai.core.errors import RateLimitedError
from nexus_ai.domain.auth.ratelimit import (
    LocalWindowRateLimiter,
    RateLimitGate,
    login_rate_key,
)

pytestmark = pytest.mark.anyio


class _Clock:
    """Deterministic injected clock for fixed-window limiter tests."""

    def __init__(self, start: dt.datetime) -> None:
        self.value = start

    def __call__(self) -> dt.datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += dt.timedelta(seconds=seconds)


class TestLocalWindowRateLimiter:
    async def test_allows_up_to_max_failures(self) -> None:
        now = _Clock(dt.datetime(2026, 9, 7, tzinfo=dt.UTC))
        limiter = LocalWindowRateLimiter(max_failures=3, window_seconds=60, now=now)
        for _ in range(3):
            await limiter.check("user-a")
            await limiter.record_failure("user-a")

    async def test_blocks_after_max_failures(self) -> None:
        now = _Clock(dt.datetime(2026, 9, 7, tzinfo=dt.UTC))
        limiter = LocalWindowRateLimiter(max_failures=2, window_seconds=60, now=now)
        for _ in range(2):
            await limiter.record_failure("user-a")
        with pytest.raises(RateLimitedError):
            await limiter.check("user-a")

    async def test_window_rolls_over(self) -> None:
        now = _Clock(dt.datetime(2026, 9, 7, tzinfo=dt.UTC))
        limiter = LocalWindowRateLimiter(max_failures=2, window_seconds=60, now=now)
        for _ in range(2):
            await limiter.record_failure("user-a")
        now.advance(61)
        await limiter.check("user-a")  # new window: allowed again

    async def test_reset_clears_the_counter(self) -> None:
        now = _Clock(dt.datetime(2026, 9, 7, tzinfo=dt.UTC))
        limiter = LocalWindowRateLimiter(max_failures=1, window_seconds=60, now=now)
        await limiter.record_failure("user-a")
        await limiter.reset("user-a")
        await limiter.check("user-a")  # not blocked

    async def test_keys_are_isolated_per_identity(self) -> None:
        now = _Clock(dt.datetime(2026, 9, 7, tzinfo=dt.UTC))
        limiter = LocalWindowRateLimiter(max_failures=1, window_seconds=60, now=now)
        await limiter.record_failure("user-a")
        await limiter.check("user-b")  # different identity is not blocked

    async def test_memory_is_bounded_by_max_keys(self) -> None:
        now = _Clock(dt.datetime(2026, 9, 7, tzinfo=dt.UTC))
        limiter = LocalWindowRateLimiter(max_failures=5, window_seconds=60, now=now, max_keys=4)
        for index in range(10):
            await limiter.record_failure(f"user-{index}")
        assert len(limiter._counts) <= 4

    async def test_retry_after_extension_surfaces_window(self) -> None:
        now = _Clock(dt.datetime(2026, 9, 7, tzinfo=dt.UTC))
        limiter = LocalWindowRateLimiter(max_failures=1, window_seconds=90, now=now)
        await limiter.record_failure("user-a")
        with pytest.raises(RateLimitedError) as excinfo:
            await limiter.check("user-a")
        assert excinfo.value.extensions["retry_after_seconds"] == 90


class TestRateLimitGate:
    def _settings(self, backend: str) -> AuthSettings:
        return AuthSettings(
            login_max_failures=2,
            login_failure_window_seconds=60,
            rate_limit_backend=backend,  # type: ignore[arg-type]
        )

    async def test_local_backend_works_without_cache(self) -> None:
        now = _Clock(dt.datetime(2026, 9, 7, tzinfo=dt.UTC))

        class _NoCache:
            is_connected = False

        gate = RateLimitGate(self._settings("local"), _NoCache(), now=now)  # type: ignore[arg-type]
        await gate.record_failure("k")
        await gate.record_failure("k")
        with pytest.raises(RateLimitedError):
            await gate.check("k")

    async def test_cache_failure_degrades_to_bounded_local(self) -> None:
        now = _Clock(dt.datetime(2026, 9, 7, tzinfo=dt.UTC))

        class _BrokenCache:
            is_connected = True

            @property
            def client(self) -> object:
                raise RuntimeError("cache is down")

        gate = RateLimitGate(self._settings("cache"), _BrokenCache(), now=now)  # type: ignore[arg-type]
        # Check falls back silently (availability over crash), failures still counted.
        await gate.check("k")
        await gate.record_failure("k")

    async def test_rate_limit_error_from_cache_propagates(self) -> None:
        now = _Clock(dt.datetime(2026, 9, 7, tzinfo=dt.UTC))

        class _LimitedCache:
            is_connected = True

            @property
            def client(self) -> object:
                return _LimitedClient()

        gate = RateLimitGate(self._settings("cache"), _LimitedCache(), now=now)  # type: ignore[arg-type]
        with pytest.raises(RateLimitedError):
            await gate.check("k")

    async def test_cache_backend_counts_across_gate_instances(self) -> None:
        # Cross-process semantics: the counter lives in the shared store, not in the gate.
        now = _Clock(dt.datetime(2026, 9, 7, tzinfo=dt.UTC))
        store: dict[str, str] = {}

        class _SharedCache:
            is_connected = True

            @property
            def client(self) -> object:
                return _SharedClient(store)

        first = RateLimitGate(self._settings("cache"), _SharedCache(), now=now)  # type: ignore[arg-type]
        second = RateLimitGate(self._settings("cache"), _SharedCache(), now=now)  # type: ignore[arg-type]
        await first.record_failure("k")
        await second.record_failure("k")
        with pytest.raises(RateLimitedError):
            await first.check("k")


class _SharedClient:
    def __init__(self, store: dict[str, str]) -> None:
        self._store = store

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def incr(self, key: str) -> int:
        value = int(self._store.get(key, "0")) + 1
        self._store[key] = str(value)
        return value

    async def expire(self, _key: str, _seconds: int) -> bool:
        return True

    async def delete(self, key: str) -> int:
        self._store.pop(key, None)
        return 1


class _LimitedClient:
    async def get(self, _key: str) -> str:
        return "5"

    async def incr(self, _key: str) -> int:
        return 6

    async def expire(self, _key: str, _seconds: int) -> bool:
        return True

    async def delete(self, _key: str) -> int:
        return 1


class TestLoginRateKey:
    def test_digests_identity_and_ip(self) -> None:
        first = login_rate_key("user@example.com", "1.2.3.4")
        second = login_rate_key("user@example.com", "1.2.3.4")
        other_ip = login_rate_key("user@example.com", "5.6.7.8")
        other_email = login_rate_key("other@example.com", "1.2.3.4")
        assert first == second
        assert first != other_ip
        assert first != other_email
        assert "@" not in first  # no PII in limiter keys
