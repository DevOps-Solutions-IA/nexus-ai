"""OpenAPI accuracy (MASTER PROMPT 002 section 37)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio


async def test_openapi_describes_implemented_surface(app_client) -> None:
    schema = (await app_client.get("/openapi.json")).json()
    assert schema["info"]["title"] == "Nexus AI"
    assert schema["info"]["version"] == "0.1.0"
    paths = set(schema["paths"])
    assert {"/health/live", "/health/ready", "/api/v1/system/version"} <= paths
    # The P03 authentication surface is advertised (login/refresh/logout/session/
    # memberships); later-phase domains are not.
    assert {
        "/api/v1/auth/login",
        "/api/v1/auth/refresh",
        "/api/v1/auth/logout",
        "/api/v1/auth/me",
        "/api/v1/auth/memberships",
    } <= paths
    # Later-phase domains are not advertised yet (P13 agents, human/user admin).
    assert not any(segment in path for path in paths for segment in ("/agents", "/users"))
    # The P09 messaging surface is advertised (accounts + one send endpoint + webhooks);
    # ``/messages`` only ever appears under ``/messaging/``.
    assert "/api/v1/messaging/messages" in paths
    assert "/api/v1/webhooks/messaging/{provider}/{token}" in paths
    assert all("/messages" not in path or "/messaging/" in path for path in paths)
    # The P05 provisioning and P06 customer/conversation surfaces are advertised;
    # arbitrary-id administration is not.
    assert "/api/v1/organizations" in paths
    assert "/api/v1/organizations/current/provisioning" in paths
    assert "/api/v1/organizations/current/settings" in paths
    assert "/api/v1/organizations/current/dashboard-schema" in paths
    assert "/api/v1/customers" in paths
    assert "/api/v1/customers/{customer_id}" in paths
    assert "/api/v1/customers/{customer_id}/identities" in paths
    assert "/api/v1/customers/{customer_id}/conversations" in paths
    assert "/api/v1/customers/{customer_id}/timeline" in paths
    assert "/api/v1/conversations" in paths
    assert "/api/v1/conversations/{conversation_id}" in paths
    assert not any(path.rstrip("/").endswith("/organizations/{organization_id}") for path in paths)


async def test_operation_ids_are_deterministic_and_unique(app_client) -> None:
    schema = (await app_client.get("/openapi.json")).json()
    operation_ids = [
        operation["operationId"]
        for path in schema["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict) and "operationId" in operation
    ]
    assert len(operation_ids) == len(set(operation_ids))


async def test_error_schema_is_published(app_client) -> None:
    schema = (await app_client.get("/openapi.json")).json()
    ready = schema["paths"]["/health/ready"]["get"]["responses"]
    assert "503" in ready
