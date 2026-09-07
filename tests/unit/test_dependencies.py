"""Dependency-injection error paths (NXS-API-001, section 45)."""

from __future__ import annotations

import pytest

from nexus_ai.api import dependencies
from nexus_ai.core.errors import InternalError

pytestmark = pytest.mark.anyio


class _FakeState:
    pass


class _FakeApp:
    def __init__(self) -> None:
        self.state = _FakeState()


class _FakeRequest:
    def __init__(self) -> None:
        self.app = _FakeApp()
        self.headers: dict[str, str] = {}


def test_get_lifespan_without_state_raises() -> None:
    with pytest.raises(InternalError):
        dependencies.get_lifespan(_FakeRequest())  # type: ignore[arg-type]


def test_get_bootstrap_settings_without_state_raises() -> None:
    with pytest.raises(InternalError):
        dependencies.get_bootstrap_settings(_FakeRequest())  # type: ignore[arg-type]


def test_get_request_metadata_requires_context() -> None:
    with pytest.raises(InternalError):
        dependencies.get_request_metadata(_FakeRequest())  # type: ignore[arg-type]


async def test_system_health_returns_503_when_not_ready(make_client, monkeypatch) -> None:
    from nexus_ai.core.health import DependencyHealth, HealthReport, HealthStatus

    async with make_client(NXS_HEALTH__CACHE_TTL_SECONDS="0") as client:
        evaluator = client.nexus_app.state.lifespan.resources.readiness

        async def _down() -> HealthReport:
            return HealthReport(
                ready=False,
                dependencies=(DependencyHealth("postgresql", HealthStatus.DOWN, True),),
            )

        monkeypatch.setattr(evaluator, "evaluate", _down)
        response = await client.get("/api/v1/system/health")
    assert response.status_code == 503
    assert response.json()["status"] == "NOT_READY"
