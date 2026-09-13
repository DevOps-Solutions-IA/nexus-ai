"""Authenticated Scheduler API, RBAC and RFC 9457 contracts."""

import datetime as dt
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


async def _published(owner: _Client) -> str:
    created = await owner.request(
        "POST",
        "/workflows",
        {
            "workflow_key": f"scheduler.api.{uuid.uuid4().hex[:8]}",
            "name": "Scheduled API workflow",
            "steps": [{"key": "done", "step_type": "NOOP", "config": {"kind": "NOOP"}}],
        },
    )
    assert created.status_code == 201, created.text
    published = await owner.request("POST", f"/workflows/{created.json()['id']}/publish")
    assert published.status_code == 200, published.text
    return str(published.json()["id"])


async def test_scheduler_api_lifecycle_and_least_privilege(
    auth_client: Any, make_auth_org: Any, make_auth_user: Any
) -> None:
    organization = await make_auth_org()
    owner = _Client(
        auth_client, await _token(auth_client, make_auth_user, organization, RoleKey.ORG_OWNER)
    )
    member = _Client(
        auth_client, await _token(auth_client, make_auth_user, organization, RoleKey.ORG_MEMBER)
    )
    version_id = await _published(owner)
    payload = {
        "schedule_key": f"api.{uuid.uuid4().hex[:8]}",
        "workflow_version_id": version_id,
        "schedule_type": "ONE_TIME",
        "timezone": "America/Bogota",
        "start_at": (dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).isoformat(),
    }
    created = await owner.request("POST", "/schedules", payload)
    assert created.status_code == 201, created.text
    schedule_id = created.json()["id"]
    transitions = await owner.request("GET", f"/schedules/{schedule_id}/transitions")
    assert transitions.status_code == 200
    assert transitions.json()[0]["correlation_id"] is not None
    assert (await member.request("GET", "/schedules")).status_code == 200
    denied = await member.request("POST", "/schedules", payload | {"schedule_key": "denied.key"})
    assert denied.status_code == 403
    assert denied.json()["code"] == "NXS_CORE_PERMISSION_DENIED"
    active = await member.request("POST", f"/schedules/{schedule_id}/activate")
    assert active.status_code == 200 and active.json()["state"] == "ACTIVE"
    paused = await member.request("POST", f"/schedules/{schedule_id}/pause")
    assert paused.status_code == 200 and paused.json()["state"] == "PAUSED"
    resumed = await member.request("POST", f"/schedules/{schedule_id}/resume")
    assert resumed.status_code == 200 and resumed.json()["state"] == "ACTIVE"
    cancelled = await member.request("POST", f"/schedules/{schedule_id}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "CANCELLED"


async def test_scheduler_api_rejects_spoofing_and_arbitrary_execution(
    auth_client: Any, make_auth_org: Any, make_auth_user: Any
) -> None:
    organization = await make_auth_org()
    owner = _Client(
        auth_client, await _token(auth_client, make_auth_user, organization, RoleKey.ORG_OWNER)
    )
    version_id = await _published(owner)
    base = {
        "schedule_key": "safe.boundary",
        "workflow_version_id": version_id,
        "schedule_type": "ONE_TIME",
        "timezone": "UTC",
        "start_at": (dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).isoformat(),
    }
    for field, value in (
        ("organization_id", str(uuid.uuid4())),
        ("url", "https://attacker.invalid"),
        ("command", "rm -rf /"),
        ("sql", "DROP TABLE organizations"),
        ("tool_key", "direct.tool"),
        ("agent_id", str(uuid.uuid4())),
    ):
        response = await owner.request("POST", "/schedules", base | {field: value})
        assert response.status_code == 422
        assert response.json()["code"] == "NXS_CORE_VALIDATION_FAILED"
