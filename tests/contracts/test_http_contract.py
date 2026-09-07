"""HTTP contract assertions (MASTER PROMPT 002 section 52)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio


async def test_health_live(app_client) -> None:
    response = await app_client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


async def test_health_ready_is_200_when_nothing_required(app_client) -> None:
    response = await app_client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "READY"
    assert isinstance(body["dependencies"], list)


async def test_legacy_health_and_version_preserved(app_client) -> None:
    health = await app_client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    version = await app_client.get("/version")
    assert version.status_code == 200
    assert version.json() == {"product": "Nexus AI", "version": "0.1.0"}


async def test_system_version_metadata(app_client) -> None:
    response = await app_client.get("/api/v1/system/version")
    body = response.json()
    assert body["product"] == "Nexus AI"
    assert body["api_version"] == "v1"
    assert body["runtime"].startswith("python-")
    assert "secret" not in body and "dsn" not in body


async def test_request_id_generated_and_echoed(app_client) -> None:
    response = await app_client.get("/health/live")
    request_id = response.headers["x-request-id"]
    assert len(request_id) == 32
    assert response.headers["x-correlation-id"] == request_id


async def test_incoming_request_id_is_honoured_when_valid(app_client) -> None:
    response = await app_client.get("/health/live", headers={"X-Request-ID": "trusted-id-000123"})
    assert response.headers["x-request-id"] == "trusted-id-000123"


async def test_pathological_request_id_is_replaced(app_client) -> None:
    response = await app_client.get("/health/live", headers={"X-Request-ID": "bad id;drop"})
    assert response.headers["x-request-id"] != "bad id;drop"
    assert len(response.headers["x-request-id"]) == 32


async def test_correlation_id_independent_of_request_id(app_client) -> None:
    response = await app_client.get(
        "/health/live", headers={"X-Correlation-ID": "corr-abcdef-0001"}
    )
    assert response.headers["x-correlation-id"] == "corr-abcdef-0001"
    assert response.headers["x-request-id"] != "corr-abcdef-0001"


async def test_unknown_route_is_problem_details_404(app_client) -> None:
    response = await app_client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == "NXS_CORE_NOT_FOUND"
    assert body["status"] == 404
    assert body["request_id"]


async def test_method_not_allowed_is_405_problem(app_client) -> None:
    response = await app_client.post("/health/live")
    assert response.status_code == 405
    assert response.json()["code"] == "NXS_CORE_METHOD_NOT_ALLOWED"


async def test_security_headers_present(app_client) -> None:
    headers = (await app_client.get("/health/live")).headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["server"] == "nexus-ai"


async def test_docs_exposed_in_non_production(app_client) -> None:
    assert (await app_client.get("/openapi.json")).status_code == 200
    assert (await app_client.get("/docs")).status_code == 200


async def test_docs_hidden_when_disabled(make_client) -> None:
    async with make_client(NXS_HTTP__DOCS_ENABLED="false") as client:
        assert (await client.get("/openapi.json")).status_code == 404
        assert (await client.get("/docs")).status_code == 404


async def test_trusted_host_enforced_when_configured(make_client) -> None:
    async with make_client(NXS_HTTP__ALLOWED_HOSTS='["allowed.example"]') as client:
        ok = await client.get("/health/live", headers={"host": "allowed.example"})
        assert ok.status_code == 200
        bad = await client.get("/health/live", headers={"host": "evil.example"})
        assert bad.status_code == 400


async def test_cors_preflight_allowed_only_for_configured_origin(make_client) -> None:
    async with make_client(NXS_HTTP__CORS_ALLOW_ORIGINS='["https://app.nexus-ai.dev"]') as client:
        allowed = await client.options(
            "/api/v1/system/version",
            headers={
                "Origin": "https://app.nexus-ai.dev",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert allowed.headers.get("access-control-allow-origin") == "https://app.nexus-ai.dev"
        denied = await client.options(
            "/api/v1/system/version",
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
        )
        assert denied.headers.get("access-control-allow-origin") is None
