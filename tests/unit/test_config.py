"""Configuration validation and redaction (NXS-CONFIG-001, MASTER PROMPT 002 section 53)."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import ValidationError

from nexus_ai.core.config import Environment, Settings, redact_url

Build = Callable[..., Settings]


def test_valid_local_configuration(build_settings: Build) -> None:
    settings = build_settings(NXS_ENVIRONMENT="local")
    assert settings.environment is Environment.LOCAL
    assert not settings.is_production
    assert settings.http.docs_enabled


def test_valid_test_configuration(build_settings: Build) -> None:
    assert build_settings().environment is Environment.TEST


def test_valid_production_configuration(build_settings: Build) -> None:
    settings = build_settings(
        NXS_ENVIRONMENT="production",
        NXS_HTTP__ALLOWED_HOSTS='["api.nexus-ai.dev"]',
        NXS_HTTP__DOCS_ENABLED="false",
        NXS_DATABASE__DSN="postgresql+asyncpg://u:p@db.internal:5432/nexus",
        NXS_CACHE__URL="rediss://cache.internal:6379/0",
        NXS_MESSAGING__URL="tls://nats.internal:4222",
        NXS_TELEMETRY__MODE="local",
    )
    assert settings.is_production
    assert settings.docs_url is None
    assert settings.openapi_url is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"NXS_ENVIRONMENT": "production"},
        {"NXS_ENVIRONMENT": "production", "NXS_HTTP__ALLOWED_HOSTS": '["*"]'},
        {
            "NXS_ENVIRONMENT": "production",
            "NXS_HTTP__ALLOWED_HOSTS": '["api.nexus-ai.dev"]',
            "NXS_HTTP__DOCS_ENABLED": "false",
            "NXS_DATABASE__DSN": "postgresql+asyncpg://u:p@db:5432/n",
            "NXS_CACHE__URL": "redis://c:6379/0",
            "NXS_MESSAGING__URL": "nats://n:4222",
            "NXS_TELEMETRY__MODE": "local",
            "NXS_HTTP__CORS_ALLOW_ORIGINS": '["*"]',
        },
    ],
)
def test_unsafe_production_configuration_fails_fast(
    build_settings: Build, overrides: dict[str, str]
) -> None:
    with pytest.raises(ValidationError):
        build_settings(**overrides)


def test_invalid_dsn_scheme_rejected(build_settings: Build) -> None:
    with pytest.raises(ValidationError, match="NXS_DATABASE__DSN"):
        build_settings(NXS_DATABASE__DSN="mysql://u:p@db:3306/n")


def test_invalid_timeout_bounds_rejected(build_settings: Build) -> None:
    with pytest.raises(ValidationError):
        build_settings(NXS_DATABASE__POOL_TIMEOUT_SECONDS="0")
    with pytest.raises(ValidationError):
        build_settings(NXS_HTTP__PORT="70000")


def test_invalid_pool_bounds_rejected(build_settings: Build) -> None:
    with pytest.raises(ValidationError):
        build_settings(NXS_DATABASE__POOL_SIZE="0")
    with pytest.raises(ValidationError):
        build_settings(NXS_CACHE__POOL_MAX_CONNECTIONS="9999")


def test_environment_overrides_apply(build_settings: Build) -> None:
    settings = build_settings(NXS_HTTP__PORT="9999", NXS_LOGGING__LEVEL="WARNING")
    assert settings.http.port == 9999
    assert settings.logging.level == "WARNING"


def test_secret_is_redacted_in_repr_and_helpers(build_settings: Build) -> None:
    settings = build_settings(
        NXS_DATABASE__DSN="postgresql+asyncpg://nexus:supersecret@db.internal:5432/nexus"
    )
    assert "supersecret" not in repr(settings)
    assert "supersecret" not in str(settings.database)
    assert settings.database.safe_dsn is not None
    assert "supersecret" not in settings.database.safe_dsn
    assert "***" in settings.database.safe_dsn
    assert settings.database.async_dsn().endswith("/nexus")


def test_redact_url_variants() -> None:
    assert redact_url("redis://:tok@h:6379/0") == "redis://:***@h:6379/0"
    assert redact_url("nats://plainhost:4222") == "nats://plainhost:4222"
    assert redact_url("postgresql://u:p@h/db") == "postgresql://u:***@h/db"


def test_cache_url_normalises_valkey_scheme(build_settings: Build) -> None:
    settings = build_settings(NXS_CACHE__URL="valkey://cache:6379/1")
    assert settings.cache.client_url().startswith("redis://")


def test_telemetry_export_requires_endpoint(build_settings: Build) -> None:
    with pytest.raises(ValidationError, match="OTLP_ENDPOINT"):
        build_settings(NXS_TELEMETRY__MODE="export")
