"""Error normalisation contract (NXS-ERROR-001, sections 33, 34)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio


async def test_application_error_renders_problem_details(make_client, add_diag_routes) -> None:
    async with make_client(routes=add_diag_routes) as client:
        response = await client.get("/_diagnostics/nxs-error")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == "NXS_CORE_NOT_FOUND"
    assert body["type"].endswith("NXS_CORE_NOT_FOUND")
    assert body["instance"] == "/_diagnostics/nxs-error"
    assert "RuntimeError" not in response.text


async def test_validation_error_is_normalised(make_client, add_diag_routes) -> None:
    async with make_client(routes=add_diag_routes) as client:
        response = await client.post("/_diagnostics/echo", json={"name": "x"})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "NXS_CORE_VALIDATION_FAILED"
    assert any(item["field"].endswith("count") for item in body["errors"])


async def test_oversized_invalid_body_is_bounded(make_client, add_diag_routes) -> None:
    async with make_client(routes=add_diag_routes) as client:
        payload = {"name": "x" * 100_000, "extra": ["y"] * 5000}
        response = await client.post("/_diagnostics/echo", json=payload)
    assert response.status_code == 422
    assert len(response.content) < 20_000


async def test_http_exception_passthrough_is_problem_shaped(make_client, add_diag_routes) -> None:
    async with make_client(routes=add_diag_routes) as client:
        response = await client.get("/_diagnostics/teapot")
    assert response.status_code == 418
    body = response.json()
    assert body["code"] == "NXS_CORE_HTTP_ERROR"
    assert body["detail"] == "I am a teapot"


async def test_request_metadata_dependency(make_client, add_diag_routes) -> None:
    async with make_client(routes=add_diag_routes) as client:
        response = await client.get(
            "/_diagnostics/meta", headers={"Idempotency-Key": "abcdef-000123"}
        )
    body = response.json()
    assert body["idempotency_key"] == "abcdef-000123"
    assert body["request_id"] == response.headers["x-request-id"]


async def test_bad_idempotency_key_is_rejected(make_client, add_diag_routes) -> None:
    async with make_client(routes=add_diag_routes) as client:
        response = await client.get("/_diagnostics/meta", headers={"Idempotency-Key": "no"})
    assert response.status_code == 400
    assert response.json()["code"] == "NXS_CORE_INVALID_REQUEST"
