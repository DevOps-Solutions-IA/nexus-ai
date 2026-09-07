"""Event platform configuration bounds and hardened-environment rules (NXS-EVENT-007)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from nexus_ai.core.config import Environment, EventsSettings, Settings


def test_defaults_are_sane() -> None:
    events = EventsSettings()
    assert events.require_jetstream is True
    assert events.publisher_batch_size == 100
    assert events.main_stream_subjects(Environment.TEST) == [
        "nxs.test.tenant.>",
        "nxs.test.global.>",
    ]
    assert events.dead_letter_subject_filter(Environment.TEST) == "nxs.test.dlq.>"


@pytest.mark.parametrize(
    "field,value",
    [
        ("publisher_batch_size", "0"),
        ("publisher_batch_size", "5000"),
        ("consumer_concurrency", "0"),
        ("max_delivery_attempts", "0"),
        ("subject_prefix", "NXS"),
        ("stream_name", "bad-name"),
    ],
)
def test_invalid_values_are_rejected(
    build_settings: Callable[..., Settings], field: str, value: str
) -> None:
    with pytest.raises(ValueError):
        build_settings(**{f"NXS_EVENTS__{field.upper()}": value})


def test_retry_base_must_not_exceed_max(build_settings: Callable[..., Settings]) -> None:
    with pytest.raises(ValueError, match="retry_base"):
        build_settings(
            NXS_EVENTS__RETRY_BASE_DELAY_SECONDS="10",
            NXS_EVENTS__RETRY_MAX_DELAY_SECONDS="5",
        )


def test_hardened_environment_requires_durable_jetstream(
    build_settings: Callable[..., Settings],
) -> None:
    with pytest.raises(ValueError, match="REQUIRE_JETSTREAM"):
        build_settings(
            NXS_ENVIRONMENT="staging",
            NXS_MESSAGING__REQUIRED="true",
            NXS_MESSAGING__URL="nats://nats.internal:4222",
            NXS_EVENTS__REQUIRE_JETSTREAM="false",
            NXS_TENANCY__HEADER_RESOLVER_ENABLED="false",
            NXS_AUTH__ALLOW_EPHEMERAL_SIGNING_KEY="false",
            NXS_AUTH__SIGNING_KEY="0" * 64,
            NXS_AUTH__ISSUER="nexus-ai",
            NXS_AUTH__AUDIENCE="nexus-ai-backend",
            NXS_AUTH__RATE_LIMIT_BACKEND="auto",
            NXS_HTTP__ALLOWED_HOSTS='["staging.nexus-ai.dev"]',
            NXS_TELEMETRY__MODE="local",
        )


def test_hardened_environment_rejects_unsafe_poll_cadence(
    build_settings: Callable[..., Settings],
) -> None:
    with pytest.raises(ValueError, match="PUBLISHER_POLL_INTERVAL"):
        build_settings(
            NXS_ENVIRONMENT="production",
            NXS_MESSAGING__REQUIRED="true",
            NXS_MESSAGING__URL="nats://nats.internal:4222",
            NXS_EVENTS__PUBLISHER_POLL_INTERVAL_SECONDS="0.05",
            NXS_TENANCY__HEADER_RESOLVER_ENABLED="false",
            NXS_AUTH__ALLOW_EPHEMERAL_SIGNING_KEY="false",
            NXS_AUTH__SIGNING_KEY="0" * 64,
            NXS_AUTH__ISSUER="nexus-ai",
            NXS_AUTH__AUDIENCE="nexus-ai-backend",
            NXS_AUTH__RATE_LIMIT_BACKEND="auto",
            NXS_HTTP__ALLOWED_HOSTS='["nexus-ai.dev"]',
            NXS_HTTP__DOCS_ENABLED="false",
            NXS_TELEMETRY__MODE="local",
            NXS_DATABASE__DSN="postgresql+asyncpg://u:p@db:5432/nx",
            NXS_CACHE__URL="redis://cache:6379/0",
        )
