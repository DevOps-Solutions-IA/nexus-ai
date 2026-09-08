"""Integration Hub HTTP API surface (NXS-INT-001)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from typing import Any

import pytest

from nexus_ai.domain.auth.rbac import RoleKey

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _owner_token(auth_client: Any, make_auth_user: Any, org: Any) -> str:
    email, password, _ = await make_auth_user(organization=org, role=RoleKey.ORG_OWNER)
    login = await auth_client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    assert login.status_code == 200, login.text
    return str(login.json()["access_token"])


def _rewire_integrations_for_loopback(auth_client: Any) -> None:
    """The running app uses the strict SSRF policy; for the API tests swap in the same
    Hub wiring the ``integration_hub`` fixture uses (loopback allowed, mock server only)."""
    from cryptography.fernet import Fernet

    from nexus_ai.domain.integrations.repository import (
        IntegrationIdempotencyRepository,
        IntegrationSecretStore,
    )
    from nexus_ai.integrations.auth_profiles import AuthProfileApplier
    from nexus_ai.integrations.circuit import CircuitBreakerRegistry
    from nexus_ai.integrations.credentials import LocalEncryptedVault, build_fernet
    from nexus_ai.integrations.destination import DestinationPolicy
    from nexus_ai.integrations.executor import GovernedHttpExecutor
    from nexus_ai.integrations.ratelimit import OutboundRateLimiter
    from nexus_ai.integrations.registry import IntegrationRegistry
    from nexus_ai.integrations.service import IntegrationHubService
    from nexus_ai.integrations.webhooks import InboundWebhookService

    resources = auth_client.nexus_app.state.lifespan.resources
    settings = resources.settings
    policy = DestinationPolicy(allow_loopback=True, resolver=lambda h, p: ["127.0.0.1"])
    vault = LocalEncryptedVault(
        IntegrationSecretStore(resources.database),
        build_fernet([Fernet.generate_key().decode()]),
    )
    executor = GovernedHttpExecutor(settings.integrations, policy)
    registry = IntegrationRegistry(
        settings, resources.database, resources.event_platform.publisher, vault, policy
    )
    resources.integration_vault = vault
    resources.integrations = registry
    resources.integration_hub = IntegrationHubService(
        settings,
        resources.database,
        resources.event_platform.publisher,
        registry,
        executor,
        AuthProfileApplier(vault, policy, executor),
        CircuitBreakerRegistry(settings.integrations),
        OutboundRateLimiter(settings.integrations, _NoCache()),
        IntegrationIdempotencyRepository(resources.database),
    )
    resources.inbound_webhooks = InboundWebhookService(
        settings, resources.database, resources.event_platform.publisher, vault
    )


class _NoCache:
    is_connected = False
    client = None


@pytest.fixture
async def api(
    auth_client: Any, make_auth_org: Any, make_auth_user: Any, mock_http_server: Any
) -> Any:
    _rewire_integrations_for_loopback(auth_client)
    org = await make_auth_org()
    token = await _owner_token(auth_client, make_auth_user, org)
    auth = {"Authorization": f"Bearer {token}"}

    class _Api:
        client = auth_client
        base = mock_http_server.base_url
        server = mock_http_server

        async def request(self, method: str, path: str, **kw: Any) -> Any:
            return await auth_client.request(
                method, f"/api/v1{path}", headers={**auth, **kw.pop("headers", {})}, **kw
            )

        async def post(self, path: str, body: Any) -> Any:
            return await self.request("POST", path, json=body)

        async def put(self, path: str, body: Any) -> Any:
            return await self.request("PUT", path, json=body)

        async def get(self, path: str) -> Any:
            return await self.request("GET", path)

        async def patch(self, path: str, body: Any) -> Any:
            return await self.request("PATCH", path, json=body)

        async def delete(self, path: str) -> Any:
            return await self.request("DELETE", path)

        async def raw(self, path: str, content: bytes) -> Any:
            return await self.request(
                "POST", path, content=content, headers={"Content-Type": "application/json"}
            )

    return _Api()


_OP_BODY = {
    "operation_key": "crm.get",
    "spec": {
        "operation_type": "REST",
        "method": "GET",
        "path": "/c/{id}",
        "path_params": {"id": {"required": True}},
        "retry_class": "SAFE",
    },
}


async def test_full_lifecycle_through_http(api: Any) -> None:
    created = await api.post(
        "/integrations",
        {"slug": "crm", "name": "CRM", "integration_type": "REST", "base_url": api.base},
    )
    assert created.status_code == 201, created.text
    integration_id = created.json()["id"]
    assert created.json()["status"] == "DRAFT"
    assert (await api.get("/integrations")).json()[0]["id"] == integration_id
    assert (await api.get(f"/integrations/{integration_id}")).json()["slug"] == "crm"
    assert (await api.patch(f"/integrations/{integration_id}", {"name": "CRM v2"})).json()[
        "name"
    ] == "CRM v2"

    op = await api.put(f"/integrations/{integration_id}/operations", _OP_BODY)
    assert op.status_code == 201, op.text
    assert len((await api.get(f"/integrations/{integration_id}/operations")).json()) == 1
    assert (await api.post(f"/integrations/{integration_id}/enable", {})).json()[
        "status"
    ] == "ACTIVE"

    api.server.set_handler(lambda m, p, h, b: (200, {"id": "c1", "name": "Ada"}))
    executed = await api.post(
        f"/integrations/{integration_id}/execute",
        {
            "integration_id": integration_id,
            "operation_key": "crm.get",
            "input": {"path_params": {"id": "c1"}},
        },
    )
    assert executed.status_code == 200, executed.text
    assert executed.json()["ok"] and executed.json()["output"] == {"id": "c1", "name": "Ada"}

    tested = await api.post(
        f"/integrations/{integration_id}/test",
        {
            "integration_id": integration_id,
            "operation_key": "crm.get",
            "input": {"path_params": {"id": "c1"}},
        },
    )
    assert tested.status_code == 200 and tested.json()["ok"]

    assert (await api.post(f"/integrations/{integration_id}/disable", {})).json()[
        "status"
    ] == "DISABLED"
    assert (
        await api.delete(f"/integrations/{integration_id}/operations/crm.get")
    ).status_code == 204


async def test_credentials_and_webhook_http(api: Any) -> None:
    created = await api.post(
        "/integrations",
        {"slug": "wh", "name": "WH", "integration_type": "WEBHOOK", "base_url": api.base},
    )
    integration_id = created.json()["id"]
    stored = await api.post(
        f"/integrations/{integration_id}/credentials",
        {
            "credential_ref": "wh:secret",
            "credential_type": "HMAC_SECRET",
            "fields": {"secret": "whsec"},
        },
    )
    assert stored.status_code == 204
    registered = await api.post(
        f"/integrations/{integration_id}/webhooks",
        {
            "slug": "orders",
            "event_type": "orders.updated",
            "signature_scheme": "HMAC_SHA256",
            "signature_header": "X-Signature",
            "timestamp_header": "X-Timestamp",
            "credential_ref": "wh:secret",
        },
    )
    assert registered.status_code == 201, registered.text
    assert registered.json()["receive_path"].startswith("/api/v1/integrations/webhooks/")
    assert (
        await api.delete(f"/integrations/{integration_id}/credentials/wh:secret")
    ).status_code == 204


async def test_openapi_import_http(api: Any) -> None:
    doc = {
        "openapi": "3.0.1",
        "info": {"title": "Imported", "version": "1"},
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/widgets/{id}": {
                "get": {
                    "operationId": "getWidget",
                    "parameters": [{"name": "id", "in": "path", "required": True}],
                }
            }
        },
    }
    response = await api.raw("/integrations/openapi/import", json.dumps(doc).encode())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "Imported"
    assert body["operations"][0]["operation_key"] == "getwidget"


async def test_execute_body_path_mismatch_is_422(api: Any) -> None:
    created = await api.post(
        "/integrations",
        {"slug": "crm", "name": "CRM", "integration_type": "REST", "base_url": api.base},
    )
    integration_id = created.json()["id"]
    bad = await api.post(
        f"/integrations/{integration_id}/execute",
        {"integration_id": str(uuid.uuid4()), "operation_key": "crm.get", "input": {}},
    )
    assert bad.status_code == 422


async def test_inbound_webhook_http_roundtrip(api: Any) -> None:
    created = await api.post(
        "/integrations",
        {"slug": "wh", "name": "WH", "integration_type": "WEBHOOK", "base_url": api.base},
    )
    integration_id = created.json()["id"]
    await api.post(
        f"/integrations/{integration_id}/credentials",
        {"credential_ref": "wh:s", "credential_type": "HMAC_SECRET", "fields": {"secret": "k"}},
    )
    registered = await api.post(
        f"/integrations/{integration_id}/webhooks",
        {
            "slug": "orders",
            "event_type": "orders.x",
            "signature_scheme": "HMAC_SHA256",
            "signature_header": "X-Signature",
            "timestamp_header": "X-Timestamp",
            "credential_ref": "wh:s",
        },
    )
    receive_path = registered.json()["receive_path"]
    body = b'{"order": 7}'
    ts = str(int(time.time()))
    sig = hmac.new(b"k", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    accepted = await api.client.post(
        receive_path,
        headers={
            "X-Signature": f"sha256={sig}",
            "X-Timestamp": ts,
            "X-Nxs-Webhook-Id": "evt-77",
            "Content-Type": "application/json",
        },
        content=body,
    )
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["accepted"] and not accepted.json()["replayed"]
    unsigned = await api.client.post(receive_path, content=body)
    assert unsigned.status_code == 401
