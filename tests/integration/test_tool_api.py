"""Tool Engine HTTP API surface (NXS-TOOL-001).

Exercises the governed registry + invocation endpoints end to end against a real app,
with the Integration Hub rewired to the loopback mock server (same approach as the P07
``test_integration_api`` suite). There is deliberately no arbitrary-execution route.
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus_ai.domain.auth.rbac import RoleKey

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


class _NoCache:
    is_connected = False
    client = None


def _rewire_for_loopback(auth_client: Any) -> None:
    """Swap the strict production SSRF policy + hub wiring for the loopback test wiring,
    then rebuild the Tool Engine on top of the rewired Integration Hub."""
    from cryptography.fernet import Fernet

    from nexus_ai.domain.integrations.repository import (
        IntegrationIdempotencyRepository,
        IntegrationSecretStore,
    )
    from nexus_ai.domain.tools.repository import ToolIdempotencyRepository
    from nexus_ai.integrations.auth_profiles import AuthProfileApplier
    from nexus_ai.integrations.circuit import CircuitBreakerRegistry
    from nexus_ai.integrations.credentials import LocalEncryptedVault, build_fernet
    from nexus_ai.integrations.destination import DestinationPolicy
    from nexus_ai.integrations.executor import GovernedHttpExecutor
    from nexus_ai.integrations.ratelimit import OutboundRateLimiter
    from nexus_ai.integrations.registry import IntegrationRegistry
    from nexus_ai.integrations.service import IntegrationHubService
    from nexus_ai.integrations.webhooks import InboundWebhookService
    from nexus_ai.tools.permissions import ToolPermissionGuard
    from nexus_ai.tools.registry import ToolRegistry
    from nexus_ai.tools.service import ToolEngine

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
    hub = IntegrationHubService(
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
    resources.integration_vault = vault
    resources.integrations = registry
    resources.integration_hub = hub
    resources.inbound_webhooks = InboundWebhookService(
        settings, resources.database, resources.event_platform.publisher, vault
    )
    tool_registry = ToolRegistry(
        settings, resources.database, resources.event_platform.publisher, registry
    )
    resources.tools = tool_registry
    resources.tool_engine = ToolEngine(
        settings,
        resources.database,
        resources.event_platform.publisher,
        tool_registry,
        hub,
        ToolPermissionGuard(resources.authorizer),
        ToolIdempotencyRepository(resources.database),
    )


async def _token(auth_client: Any, make_auth_user: Any, org: Any, role: RoleKey) -> str:
    email, password, _ = await make_auth_user(organization=org, role=role)
    login = await auth_client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    assert login.status_code == 200, login.text
    return str(login.json()["access_token"])


_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path_params"],
    "properties": {"path_params": {"type": "object"}},
}


@pytest.fixture
async def api(
    auth_client: Any, make_auth_org: Any, make_auth_user: Any, mock_http_server: Any
) -> Any:
    _rewire_for_loopback(auth_client)
    org = await make_auth_org()
    owner = await _token(auth_client, make_auth_user, org, RoleKey.ORG_OWNER)

    auth_headers = {"Authorization": f"Bearer {owner}"}

    class _Api:
        client = auth_client
        server = mock_http_server
        base = mock_http_server.base_url
        auth = auth_headers
        organization = org

        async def request(self, method: str, path: str, **kw: Any) -> Any:
            headers = {**self.auth, **kw.pop("headers", {})}
            return await auth_client.request(method, f"/api/v1{path}", headers=headers, **kw)

        async def post(self, path: str, body: Any, **kw: Any) -> Any:
            return await self.request("POST", path, json=body, **kw)

        async def get(self, path: str, **kw: Any) -> Any:
            return await self.request("GET", path, **kw)

        async def patch(self, path: str, body: Any) -> Any:
            return await self.request("PATCH", path, json=body)

        async def delete(self, path: str) -> Any:
            return await self.request("DELETE", path)

    return _Api()


async def _ready_integration(api: Any) -> str:
    created = await api.post(
        "/integrations",
        {"slug": "crm", "name": "CRM", "integration_type": "REST", "base_url": api.base},
    )
    assert created.status_code == 201, created.text
    integration_id = created.json()["id"]
    op = await api.request(
        "PUT",
        f"/integrations/{integration_id}/operations",
        json={
            "operation_key": "crm.get",
            "spec": {
                "operation_type": "REST",
                "method": "GET",
                "path": "/c/{id}",
                "path_params": {"id": {"required": True}},
                "retry_class": "SAFE",
            },
        },
    )
    assert op.status_code == 201, op.text
    assert (await api.post(f"/integrations/{integration_id}/enable", {})).json()["status"] == (
        "ACTIVE"
    )
    return str(integration_id)


async def test_tool_lifecycle_and_invocation_through_http(api: Any) -> None:
    integration_id = await _ready_integration(api)

    created = await api.post(
        "/tools",
        {
            "tool_key": "crm.get_contact",
            "name": "Get contact",
            "risk_class": "LOW",
            "input_schema": _INPUT_SCHEMA,
            "binding": {"integration_id": integration_id, "operation_key": "crm.get"},
        },
    )
    assert created.status_code == 201, created.text
    tool = created.json()
    assert tool["status"] == "DRAFT" and tool["version"] == 1
    tool_id = tool["id"]

    assert (await api.get("/tools")).json()[0]["id"] == tool_id
    assert (await api.get(f"/tools/{tool_id}")).json()["tool_key"] == "crm.get_contact"

    patched = await api.patch(f"/tools/{tool_id}", {"description": "Fetch one CRM contact"})
    assert patched.json()["description"] == "Fetch one CRM contact"

    enabled = await api.post(f"/tools/{tool_id}/enable", {})
    assert enabled.json()["status"] == "ACTIVE"

    api.server.set_handler(lambda m, p, h, b: (200, {"id": "c1", "name": "Ada"}))
    invoked = await api.post(
        "/tools/invoke",
        {"tool_key": "crm.get_contact", "arguments": {"path_params": {"id": "c1"}}},
    )
    assert invoked.status_code == 200, invoked.text
    body = invoked.json()
    assert body["ok"] and body["result_class"] == "SUCCESS"
    assert body["output"] == {"id": "c1", "name": "Ada"}
    # the payload never carried an integration id / url / method — only tool_key + args
    assert "integration_id" not in body and "url" not in body

    disabled = await api.post(f"/tools/{tool_id}/disable", {})
    assert disabled.json()["status"] == "DISABLED"

    blocked = await api.post(
        "/tools/invoke",
        {"tool_key": "crm.get_contact", "arguments": {"path_params": {"id": "c1"}}},
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["type"].endswith("NXS_TOOL_DISABLED")

    assert (await api.delete(f"/tools/{tool_id}")).status_code == 204
    assert (await api.get(f"/tools/{tool_id}")).status_code == 404


async def test_invoke_rejects_unknown_arguments_through_http(api: Any) -> None:
    integration_id = await _ready_integration(api)
    created = await api.post(
        "/tools",
        {
            "tool_key": "crm.get_contact",
            "name": "Get contact",
            "input_schema": _INPUT_SCHEMA,
            "binding": {"integration_id": integration_id, "operation_key": "crm.get"},
        },
    )
    await api.post(f"/tools/{created.json()['id']}/enable", {})
    bad = await api.post(
        "/tools/invoke",
        {
            "tool_key": "crm.get_contact",
            "arguments": {"path_params": {"id": "c1"}, "url": "http://evil.example"},
        },
    )
    assert bad.status_code == 422, bad.text
    assert bad.json()["type"].endswith("NXS_TOOL_ARGS_INVALID")


async def test_member_cannot_manage_but_can_invoke(
    api: Any, auth_client: Any, make_auth_user: Any
) -> None:
    integration_id = await _ready_integration(api)
    owner_created = await api.post(
        "/tools",
        {
            "tool_key": "crm.get_contact",
            "name": "Get contact",
            "input_schema": _INPUT_SCHEMA,
            "binding": {"integration_id": integration_id, "operation_key": "crm.get"},
        },
    )
    tool_id = owner_created.json()["id"]
    await api.post(f"/tools/{tool_id}/enable", {})

    # a member of the SAME org
    member = await _token(auth_client, make_auth_user, api.organization, RoleKey.ORG_MEMBER)
    mheaders = {"Authorization": f"Bearer {member}"}

    denied = await auth_client.post(
        "/api/v1/tools",
        headers=mheaders,
        json={
            "tool_key": "x.y",
            "name": "x",
            "input_schema": {"type": "object", "additionalProperties": False},
            "binding": {"integration_id": integration_id, "operation_key": "crm.get"},
        },
    )
    assert denied.status_code == 403, denied.text

    api.server.set_handler(lambda m, p, h, b: (200, {"id": "c1"}))
    ok = await auth_client.post(
        "/api/v1/tools/invoke",
        headers=mheaders,
        json={"tool_key": "crm.get_contact", "arguments": {"path_params": {"id": "c1"}}},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["ok"]
