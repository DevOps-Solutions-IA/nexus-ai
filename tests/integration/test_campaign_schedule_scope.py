"""NXS-P16 one-time campaign scheduling API boundary."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest

from nexus_ai.domain.auth.rbac import RoleKey

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_campaign_schedule_api_rejects_recurring_with_governed_validation_error(
    auth_client: Any, make_auth_org: Any, make_auth_user: Any
) -> None:
    organization = await make_auth_org()
    email, password, _ = await make_auth_user(organization=organization, role=RoleKey.ORG_OWNER)
    login = await auth_client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    assert login.status_code == 200

    response = await auth_client.post(
        f"/api/v1/campaigns/{uuid.uuid7()}/schedule",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        json={
            "schedule_type": "RECURRING",
            "timezone": "UTC",
            "start_at": (dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).isoformat(),
            "recurrence": {"frequency": "MINUTELY"},
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "NXS_CORE_VALIDATION_FAILED"
    assert "Campaign scheduling supports ONE_TIME only" in response.text
