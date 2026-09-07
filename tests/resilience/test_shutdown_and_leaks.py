"""Graceful shutdown and resource-leak checks (MASTER PROMPT 002 sections 39, 51)."""

from __future__ import annotations

import gc
import warnings
from collections.abc import Callable

import pytest

from nexus_ai.core.config import Settings
from nexus_ai.core.lifecycle import ApplicationLifespan

pytestmark = pytest.mark.anyio


async def test_shutdown_disposes_all_resources(build_settings: Callable[..., Settings]) -> None:
    lifespan = ApplicationLifespan(build_settings())
    resources = await lifespan.startup()
    await lifespan.shutdown()
    assert not resources.database.is_connected
    assert not resources.cache.is_connected
    assert not resources.messaging.is_connected


async def test_repeated_lifespan_cycles_do_not_accumulate_resources(
    build_settings: Callable[..., Settings],
) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        for _ in range(5):
            lifespan = ApplicationLifespan(build_settings())
            await lifespan.startup()
            await lifespan.shutdown()
            await lifespan.shutdown()
    gc.collect()


async def test_shutdown_is_safe_before_startup(build_settings: Callable[..., Settings]) -> None:
    lifespan = ApplicationLifespan(build_settings())
    await lifespan.shutdown()  # must not raise even though startup never ran
