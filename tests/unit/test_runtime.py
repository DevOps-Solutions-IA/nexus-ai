"""Application factory guarantees (NXS-PLATFORM-002, MASTER PROMPT 002 sections 17, 38)."""

from __future__ import annotations

import sys

import pytest

from nexus_ai.application import create_app
from nexus_ai.core.config import Settings
from nexus_ai.core.lifecycle import ApplicationLifespan

pytestmark = pytest.mark.anyio


def test_importing_main_opens_no_connections(build_settings) -> None:
    build_settings()
    sys.modules.pop("nexus_ai.main", None)
    import nexus_ai.main as main

    assert main.app is not None
    lifespan: ApplicationLifespan = main.app.state.lifespan
    assert lifespan._resources is None  # startup has not run


def test_factory_produces_independent_apps(build_settings) -> None:
    settings = build_settings()
    first = create_app(settings)
    second = create_app(settings)
    assert first is not second
    assert first.state.lifespan is not second.state.lifespan


async def test_lifespan_startup_and_idempotent_shutdown(build_settings) -> None:
    settings: Settings = build_settings()
    lifespan = ApplicationLifespan(settings)
    resources = await lifespan.startup()
    assert resources.metadata.product == "Nexus AI"
    await lifespan.shutdown()
    await lifespan.shutdown()  # must not raise


async def test_health_live_needs_no_resources(app_client) -> None:
    response = await app_client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}
