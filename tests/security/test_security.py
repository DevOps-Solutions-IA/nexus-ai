"""Security assertions (MASTER PROMPT 002 sections 36, 70)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

pytestmark = pytest.mark.anyio


async def test_secret_fields_never_appear_in_settings_repr(build_settings) -> None:
    settings = build_settings(
        NXS_DATABASE__DSN="postgresql+asyncpg://nexus:topsecret@db:5432/n",
        NXS_CACHE__URL="redis://:cachesecret@cache:6379/0",
    )
    blob = repr(settings) + str(settings.model_dump())
    assert "topsecret" not in blob
    assert "cachesecret" not in blob


async def test_dsn_is_redacted_in_readiness_and_metadata(make_client) -> None:
    async with make_client(
        NXS_DATABASE__REQUIRED="true",
        NXS_DATABASE__DSN="postgresql+asyncpg://nexus:leakme@127.0.0.1:5999/n",
    ) as client:
        ready = await client.get("/health/ready")
        version = await client.get("/api/v1/system/version")
        assert "leakme" not in ready.text
        assert "leakme" not in version.text


async def test_authorization_and_cookie_headers_are_not_logged(
    make_client, capsys: pytest.CaptureFixture[str]
) -> None:
    async with make_client() as client:
        await client.get(
            "/health/live",
            headers={"Authorization": "Bearer secret-token", "Cookie": "session=abc123"},
        )
    output = capsys.readouterr().out
    assert "secret-token" not in output
    assert "abc123" not in output


async def test_unexpected_error_returns_safe_problem_without_stack_trace(
    make_client, add_boom_route, capsys: pytest.CaptureFixture[str]
) -> None:
    async with make_client(routes=add_boom_route) as client:
        response = await client.get("/_diagnostics/boom")
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "NXS_CORE_INTERNAL"
    assert "Traceback" not in response.text
    assert "hunter2" not in response.text
    assert body["request_id"]
    # The internal log records the failure but not the leaked credential.
    assert "hunter2" not in capsys.readouterr().out


async def test_production_wildcard_cors_is_rejected(build_settings) -> None:
    with pytest.raises(ValidationError, match="wildcard CORS"):
        build_settings(
            NXS_ENVIRONMENT="production",
            NXS_HTTP__ALLOWED_HOSTS='["api.nexus-ai.dev"]',
            NXS_HTTP__DOCS_ENABLED="false",
            NXS_DATABASE__DSN="postgresql+asyncpg://u:p@db:5432/n",
            NXS_CACHE__URL="redis://c:6379/0",
            NXS_MESSAGING__URL="nats://n:4222",
            NXS_TELEMETRY__MODE="local",
            NXS_HTTP__CORS_ALLOW_ORIGINS='["*"]',
            NXS_AUTH__ISSUER="nexus-ai",
            NXS_AUTH__AUDIENCE="nexus-ai-backend",
            NXS_AUTH__SIGNING_KEY="0" * 64,
            NXS_AUTH__ALLOW_EPHEMERAL_SIGNING_KEY="false",
            NXS_AUTH__RATE_LIMIT_BACKEND="auto",
        )


async def test_invalid_host_is_rejected_per_policy(make_client) -> None:
    async with make_client(NXS_HTTP__ALLOWED_HOSTS='["api.nexus-ai.dev"]') as client:
        response = await client.get("/health/live", headers={"host": "attacker.test"})
    assert response.status_code == 400


async def test_oversized_headers_are_rejected(make_client) -> None:
    async with make_client(NXS_HTTP__MAX_REQUEST_HEADER_BYTES="2048") as client:
        response = await client.get("/health/live", headers={"x-noise": "a" * 4096})
    assert response.status_code == 431
    assert response.headers["content-type"].startswith("application/problem+json")
