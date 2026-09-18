"""Live authentication and platform capability guard the bounded P18 control surface."""

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_cell_control_requires_platform_grant_and_ignores_forged_headers(
    auth_client: Any, make_auth_org: Any, make_auth_user: Any, login_helper: Any
) -> None:
    organization = await make_auth_org()
    email, password, user = await make_auth_user(organization=organization)
    token = (await login_helper(email, password))["access_token"]
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Cell-ID": str(uuid4()),
        "X-NXS-Organization-ID": str(uuid4()),
        "X-Assignment-Generation": "999",
    }
    payload = {
        "cell_key": f"cell-{uuid4().hex}",
        "idempotency_key": uuid4().hex,
        "reason_code": "REGISTER",
    }
    denied = await auth_client.post("/api/v1/cells", headers=headers, json=payload)
    assert denied.status_code == 403
    assert (await auth_client.get("/api/v1/cells", headers=headers)).status_code == 403
    resources = auth_client.nexus_app.state.lifespan.resources
    async with resources.database.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO platform_grants (user_id, capability) VALUES (:user, 'cell:control')"
            ),
            {"user": user.id},
        )
    created = await auth_client.post("/api/v1/cells", headers=headers, json=payload)
    assert created.status_code == 201, created.text
    cell = created.json()
    assert cell["id"] != headers["X-Cell-ID"]
    assert (await auth_client.post("/api/v1/cells", headers=headers, json=payload)).json() == cell
    assert (await auth_client.get(f"/api/v1/cells/{cell['id']}", headers=headers)).json() == cell
    listed = await auth_client.get("/api/v1/cells?limit=1", headers=headers)
    assert listed.status_code == 200 and len(listed.json()) == 1
    for query in ("limit=101", "after_id=malformed"):
        assert (await auth_client.get(f"/api/v1/cells?{query}", headers=headers)).status_code == 422
    for field in ("organization_id", "url", "metadata", "command"):
        invalid = await auth_client.post(
            "/api/v1/cells", headers=headers, json=payload | {field: "forbidden"}
        )
        assert invalid.status_code == 422
    assignment = {
        "cell_id": cell["id"],
        "operation": "ASSIGN",
        "idempotency_key": uuid4().hex,
        "reason_code": "ASSIGN",
    }
    placed = await auth_client.post(
        "/api/v1/cell-placement/current", headers=headers, json=assignment
    )
    assert placed.status_code == 200, placed.text
    assert placed.json()["organization_id"] == str(organization.id)
    assert (
        await auth_client.get("/api/v1/cell-placement/current", headers=headers)
    ).json() == placed.json()
    retire = await auth_client.post(
        "/api/v1/cells/retire",
        headers=headers,
        json={"cell_id": cell["id"], "idempotency_key": uuid4().hex, "reason_code": "RETIRE"},
    )
    assert retire.status_code == 409
    assert "Traceback" not in retire.text


async def test_unbound_cell_can_retire_through_authenticated_control(
    auth_client: Any, make_auth_org: Any, make_auth_user: Any, login_helper: Any
) -> None:
    organization = await make_auth_org()
    email, password, user = await make_auth_user(organization=organization)
    headers = {"Authorization": f"Bearer {(await login_helper(email, password))['access_token']}"}
    resources = auth_client.nexus_app.state.lifespan.resources
    async with resources.database.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO platform_grants (user_id, capability) VALUES (:user, 'cell:control')"
            ),
            {"user": user.id},
        )
    created = await auth_client.post(
        "/api/v1/cells",
        headers=headers,
        json={
            "cell_key": f"retire-{uuid4().hex}",
            "idempotency_key": uuid4().hex,
            "reason_code": "REGISTER",
        },
    )
    assert created.status_code == 201
    payload = {
        "cell_id": created.json()["id"],
        "idempotency_key": uuid4().hex,
        "reason_code": "RETIRE",
    }
    first = await auth_client.post("/api/v1/cells/retire", headers=headers, json=payload)
    assert first.status_code == 200 and first.json()["state"] == "RETIRED"
    assert (
        await auth_client.post("/api/v1/cells/retire", headers=headers, json=payload)
    ).json() == first.json()
