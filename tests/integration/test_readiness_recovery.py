"""Readiness degrades and recovers without a process restart (section 49)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from nexus_ai.core.config import Settings
from nexus_ai.core.health import HealthStatus, ReadinessEvaluator
from nexus_ai.infrastructure.cache import Cache

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_readiness_recovers_when_dependency_returns(
    integration_env: Callable[..., Settings],
) -> None:
    settings = integration_env()
    live = Cache(settings.cache)
    await live.connect()
    dead = Cache(
        settings.model_copy(update={"cache": settings.cache.model_copy(update={"url": None})}).cache
    )

    state = {"healthy": False}

    async def probe():
        target = live if state["healthy"] else dead
        return await target.probe(timeout=3)

    evaluator = ReadinessEvaluator([probe], ttl_seconds=0, probe_timeout=3)
    try:
        first = await evaluator.evaluate()
        assert not first.ready
        assert first.dependencies[0].status is HealthStatus.DOWN

        state["healthy"] = True
        recovered = await evaluator.evaluate()
        assert recovered.ready
        assert recovered.dependencies[0].status is HealthStatus.UP
    finally:
        await live.disconnect()


async def test_migrations_apply_cleanly(
    integration_env: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`alembic upgrade head` + `alembic check` succeed against the framework baseline."""
    from pathlib import Path

    import anyio
    from alembic import command
    from alembic.config import Config

    settings = integration_env()
    monkeypatch.setenv("NXS_DATABASE__DSN", settings.database.async_dsn())
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))

    def _run() -> None:
        command.upgrade(config, "head")
        command.check(config)

    await anyio.to_thread.run_sync(_run)
