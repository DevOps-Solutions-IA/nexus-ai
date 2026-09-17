"""Authenticated P17 API, least-privilege RBAC and strict request contracts."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from nexus_ai.domain.auth.rbac import RoleKey

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _token(auth_client: Any, make_auth_user: Any, organization: Any, role: RoleKey) -> str:
    email, password, _ = await make_auth_user(organization=organization, role=role)
    response = await auth_client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_human_api_enforces_least_privilege(
    auth_client: Any, make_auth_org: Any, make_auth_user: Any
) -> None:
    organization = await make_auth_org()
    owner = await _token(auth_client, make_auth_user, organization, RoleKey.ORG_OWNER)
    member = await _token(auth_client, make_auth_user, organization, RoleKey.ORG_MEMBER)
    payload = {
        "queue_key": f"api-{uuid.uuid4().hex[:8]}",
        "name": "API Support",
        "supported_channels": ["SMS"],
        "max_active_assignments": 2,
    }
    created = await auth_client.post("/api/v1/human-queues", headers=_headers(owner), json=payload)
    assert created.status_code == 201, created.text
    listed = await auth_client.get("/api/v1/human-queues", headers=_headers(member))
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [created.json()["id"]]
    presence = await auth_client.put(
        "/api/v1/human-agents/me/presence",
        headers=_headers(member),
        json={"state": "AVAILABLE", "capacity": 2},
    )
    assert presence.status_code == 200, presence.text
    denied = await auth_client.post(
        "/api/v1/human-queues",
        headers=_headers(member),
        json=payload | {"queue_key": "member-denied"},
    )
    assert denied.status_code == 403
    assert denied.json()["permission"] == "human:configure"
    supervised = await auth_client.post(
        f"/api/v1/human-work-items/{uuid.uuid7()}/cancel",
        headers=_headers(member),
        json={"reason_code": "SUPERVISOR_CANCELLED"},
    )
    assert supervised.status_code == 403
    assert supervised.json()["permission"] == "human:supervise"


async def test_human_api_rejects_tenant_spoofing_and_executable_fields(
    auth_client: Any, make_auth_org: Any, make_auth_user: Any
) -> None:
    organization = await make_auth_org()
    owner = await _token(auth_client, make_auth_user, organization, RoleKey.ORG_OWNER)
    base = {
        "queue_key": "strict-boundary",
        "name": "Strict Boundary",
        "supported_channels": ["EMAIL"],
    }
    for field, value in (
        ("organization_id", str(uuid.uuid7())),
        ("sql", "SELECT * FROM customers"),
        ("command", "rm -rf /"),
        ("provider_url", "https://attacker.invalid"),
    ):
        response = await auth_client.post(
            "/api/v1/human-queues",
            headers=_headers(owner),
            json=base | {field: value},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "NXS_CORE_VALIDATION_FAILED"


async def test_human_api_requires_trusted_tenant_context(auth_client: Any) -> None:
    response = await auth_client.get("/api/v1/human-queues")
    assert response.status_code == 403
    assert response.json()["code"] == "NXS_TENANT_CONTEXT_REQUIRED"
