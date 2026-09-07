"""Fail-closed runtime-role verification (NXS-SEC-003, independent audit finding).

Staging/production must treat an inability to verify the runtime database role as a
security failure, exactly like finding a superuser / BYPASSRLS role. Local/test may
log a warning and continue.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from nexus_ai.core.config import Settings
from nexus_ai.core.errors import ConfigurationError
from nexus_ai.core.lifecycle import ApplicationLifespan
from nexus_ai.infrastructure.database import RuntimeRoleReport

pytestmark = pytest.mark.anyio

Build = Callable[..., Settings]

_HARDENED_BASE = {
    "NXS_HTTP__ALLOWED_HOSTS": '["api.nexus-ai.dev"]',
    "NXS_HTTP__DOCS_ENABLED": "false",
    "NXS_DATABASE__DSN": "postgresql+asyncpg://nexus_runtime:x@db.internal:5432/nexus",
    "NXS_CACHE__URL": "redis://cache.internal:6379/0",
    "NXS_MESSAGING__URL": "tls://nats.internal:4222",
    "NXS_TELEMETRY__MODE": "local",
}


class _FakeDatabase:
    def __init__(self, *, report: RuntimeRoleReport | None = None, error: Exception | None = None):
        self._report = report
        self._error = error
        self.is_connected = True

    async def runtime_role_report(self) -> RuntimeRoleReport:
        if self._error is not None:
            raise self._error
        assert self._report is not None
        return self._report


def _safe_report() -> RuntimeRoleReport:
    return RuntimeRoleReport("nexus_runtime", False, False, False, False)


def _bypass_report() -> RuntimeRoleReport:
    return RuntimeRoleReport("nexus_local", True, True, True, True)


async def _verify(settings: Settings, database: _FakeDatabase) -> None:
    await ApplicationLifespan(settings)._verify_runtime_role(database)  # type: ignore[arg-type]


@pytest.mark.parametrize("environment", ["staging", "production"])
async def test_hardened_env_fails_closed_when_check_cannot_complete(
    build_settings: Build, environment: str
) -> None:
    settings = build_settings(NXS_ENVIRONMENT=environment, **_HARDENED_BASE)
    database = _FakeDatabase(error=RuntimeError("connect timeout to 10.0.0.5:5432 password=leak"))
    with pytest.raises(ConfigurationError, match="unable to verify the runtime database role"):
        await _verify(settings, database)


async def test_hardened_error_message_has_no_raw_database_detail(build_settings: Build) -> None:
    settings = build_settings(NXS_ENVIRONMENT="production", **_HARDENED_BASE)
    database = _FakeDatabase(error=RuntimeError("password=hunter2 host=10.0.0.9"))
    try:
        await _verify(settings, database)
    except ConfigurationError as exc:
        assert "hunter2" not in str(exc)
        assert "10.0.0.9" not in str(exc)
        assert exc.__cause__ is None
    else:  # pragma: no cover
        pytest.fail("expected ConfigurationError")


@pytest.mark.parametrize("environment", ["local", "test"])
async def test_local_test_env_may_warn_and_continue_when_check_fails(
    build_settings: Build, environment: str
) -> None:
    import structlog

    settings = build_settings(NXS_ENVIRONMENT=environment)
    database = _FakeDatabase(error=RuntimeError("db down 10.0.0.5 password=secret"))
    with structlog.testing.capture_logs() as events:
        await _verify(settings, database)  # must not raise
    skipped = [e for e in events if e["event"] == "runtime_role_check_skipped"]
    assert skipped and skipped[0]["error_type"] == "RuntimeError"
    assert "secret" not in str(skipped[0])


@pytest.mark.parametrize("environment", ["staging", "production"])
async def test_hardened_env_still_fails_closed_on_superuser_or_bypassrls(
    build_settings: Build, environment: str
) -> None:
    settings = build_settings(NXS_ENVIRONMENT=environment, **_HARDENED_BASE)
    database = _FakeDatabase(report=_bypass_report())
    with pytest.raises(ConfigurationError, match="can bypass tenant RLS"):
        await _verify(settings, database)


async def test_hardened_env_passes_with_a_non_bypass_role(build_settings: Build) -> None:
    settings = build_settings(NXS_ENVIRONMENT="production", **_HARDENED_BASE)
    await _verify(settings, _FakeDatabase(report=_safe_report()))  # must not raise


async def test_local_env_warns_but_continues_on_bypass_role(build_settings: Build) -> None:
    import structlog

    settings = build_settings(NXS_ENVIRONMENT="local")
    with structlog.testing.capture_logs() as events:
        await _verify(settings, _FakeDatabase(report=_bypass_report()))  # must not raise
    assert any(e["event"] == "runtime_role_can_bypass_rls" for e in events)
