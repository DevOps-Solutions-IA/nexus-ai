"""Workflow HTTP contracts with real authentication, RBAC, RLS and PostgreSQL."""

import uuid
from typing import Any

import pytest

from nexus_ai.domain.auth.rbac import RoleKey

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _token(auth_client: Any, make_auth_user: Any, org: Any, role: RoleKey) -> str:
    email, password, _ = await make_auth_user(organization=org, role=role)
    response = await auth_client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200
    return str(response.json()["access_token"])


class _Client:
    def __init__(self, http: Any, token: str) -> None:
        self._http = http
        self._headers = {"Authorization": f"Bearer {token}"}

    async def request(self, method: str, path: str, body: Any = None) -> Any:
        options: dict[str, Any] = {"headers": self._headers}
        if method in {"POST", "PATCH"}:
            options["json"] = body or {}
        return await self._http.request(method, f"/api/v1{path}", **options)


@pytest.fixture
async def workflow_api(auth_client: Any, make_auth_org: Any, make_auth_user: Any) -> Any:
    organization = await make_auth_org()
    owner = await _token(auth_client, make_auth_user, organization, RoleKey.ORG_OWNER)
    member = await _token(auth_client, make_auth_user, organization, RoleKey.ORG_MEMBER)
    return _Client(auth_client, owner), _Client(auth_client, member)


def _definition() -> dict[str, Any]:
    return {
        "workflow_key": f"api.{uuid.uuid4().hex[:10]}",
        "name": "API workflow",
        "steps": [
            {
                "key": "finish",
                "step_type": "NOOP",
                "config": {"kind": "NOOP", "output": {"ok": True}},
            }
        ],
    }


async def test_owner_definition_and_run_lifecycle(workflow_api: Any) -> None:
    owner, _member = workflow_api
    created = await owner.request("POST", "/workflows", _definition())
    assert created.status_code == 201, created.text
    definition = created.json()
    assert (await owner.request("GET", "/workflows")).status_code == 200
    assert (await owner.request("GET", f"/workflows/{definition['id']}")).status_code == 200

    updated = await owner.request(
        "PATCH",
        f"/workflows/{definition['id']}",
        {"expected_revision": 1, "name": "Updated workflow"},
    )
    assert updated.status_code == 200 and updated.json()["revision"] == 2
    published = await owner.request("POST", f"/workflows/{definition['id']}/publish")
    assert published.status_code == 200, published.text
    version = published.json()
    assert (
        await owner.request("GET", f"/workflows/{definition['id']}/versions")
    ).status_code == 200
    assert (
        await owner.request("GET", f"/workflows/{definition['id']}/versions/{version['id']}")
    ).status_code == 200

    started = await owner.request(
        "POST",
        "/workflow-runs",
        {
            "workflow_version_id": version["id"],
            "input": {"safe": True},
            "idempotency_key": "http-workflow-run",
        },
    )
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]
    assert (await owner.request("GET", "/workflow-runs")).status_code == 200
    assert (await owner.request("GET", f"/workflow-runs/{run_id}")).status_code == 200
    assert (await owner.request("GET", f"/workflow-runs/{run_id}/steps")).status_code == 200
    assert (await owner.request("GET", f"/workflow-runs/{run_id}/transitions")).status_code == 200
    paused = await owner.request("POST", f"/workflow-runs/{run_id}/pause")
    assert paused.status_code == 200 and paused.json()["state"] == "PAUSED"
    resumed = await owner.request("POST", f"/workflow-runs/{run_id}/resume")
    assert resumed.status_code == 200 and resumed.json()["state"] == "RUNNING"
    cancelled = await owner.request("POST", f"/workflow-runs/{run_id}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "CANCELLED"


async def test_member_can_read_and_execute_but_cannot_configure(workflow_api: Any) -> None:
    owner, member = workflow_api
    created = await owner.request("POST", "/workflows", _definition())
    assert created.status_code == 201
    definition_id = created.json()["id"]
    published = await owner.request("POST", f"/workflows/{definition_id}/publish")
    assert published.status_code == 200

    assert (await member.request("GET", "/workflows")).status_code == 200
    started = await member.request(
        "POST", "/workflow-runs", {"workflow_version_id": published.json()["id"]}
    )
    assert started.status_code == 201
    denied = await member.request("POST", "/workflows", _definition())
    assert denied.status_code == 403
    assert denied.json()["code"] == "NXS_CORE_PERMISSION_DENIED"


async def test_workflow_request_unknown_field_is_rfc_problem(workflow_api: Any) -> None:
    owner, _member = workflow_api
    payload = _definition()
    payload["url"] = "https://attacker.invalid"
    response = await owner.request("POST", "/workflows", payload)
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "NXS_CORE_VALIDATION_FAILED"
