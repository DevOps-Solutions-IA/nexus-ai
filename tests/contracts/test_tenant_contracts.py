"""Stable Problem Details for tenant errors; no raw DB errors escape (section 78).

Since P03, the organizations API resolves scope from verified bearer tokens with
live-state validation and enforces RBAC (organization:read / organization:write), so
the contract tests authenticate real users against real Organizations.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

PASSWORD = "correct-horse-battery-staple"


async def test_tenant_context_required_is_problem_details(auth_client: Any) -> None:
    response = await auth_client.get("/api/v1/organizations/current")
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == "NXS_TENANT_CONTEXT_REQUIRED"
    assert body["type"].endswith("NXS_TENANT_CONTEXT_REQUIRED")
    assert body["request_id"]


async def test_version_conflict_is_problem_details(
    auth_client: Any, make_auth_org: Any, make_auth_user: Any, login_helper: Any
) -> None:
    org = await make_auth_org()
    email, _, _ = await make_auth_user(organization=org)
    session = await login_helper(email, PASSWORD)
    response = await auth_client.patch(
        "/api/v1/organizations/current",
        headers={"Authorization": f"Bearer {session['access_token']}"},
        json={"display_name": "New", "expected_version": 999},
    )
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "NXS_ORG_VERSION_CONFLICT"
    assert body.get("retryable") is True


async def test_inactive_organization_is_problem_details(
    auth_client: Any, make_auth_org: Any, make_auth_user: Any, login_helper: Any
) -> None:
    org = await make_auth_org()
    email, _, _ = await make_auth_user(organization=org)
    session = await login_helper(email, PASSWORD)
    from nexus_ai.domain.organizations.status import OrganizationStatus

    resources = auth_client.nexus_app.state.lifespan.resources  # type: ignore[attr-defined]
    await resources.organizations.transition(org.id, OrganizationStatus.SUSPENDED)
    response = await auth_client.patch(
        "/api/v1/organizations/current",
        headers={"Authorization": f"Bearer {session['access_token']}"},
        json={"display_name": "New", "expected_version": org.version},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "NXS_ORG_INACTIVE"


async def test_duplicate_key_conflict_has_no_raw_db_error(organization_service: Any) -> None:
    from nexus_ai.core.errors import OrganizationConflictError
    from nexus_ai.domain.organizations.entities import OrganizationDraft

    draft = OrganizationDraft(
        organization_key=f"dupe-key-org-{uuid.uuid4().hex[:8]}",
        display_name="Dupe",
        legal_name="Dupe SA",
        country_code="us",
        timezone="UTC",
    )
    await organization_service.create_core_record(draft)
    with pytest.raises(OrganizationConflictError) as caught:
        await organization_service.create_core_record(draft)
    message = str(caught.value)
    assert "IntegrityError" not in message
    assert "uq_organizations" not in message
    assert "psycopg" not in message and "asyncpg" not in message


async def test_current_organization_view_is_safe(
    auth_client: Any, make_auth_org: Any, make_auth_user: Any, login_helper: Any
) -> None:
    org = await make_auth_org()
    email, _, _ = await make_auth_user(organization=org)
    session = await login_helper(email, PASSWORD)
    body = (
        await auth_client.get(
            "/api/v1/organizations/current",
            headers={"Authorization": f"Bearer {session['access_token']}"},
        )
    ).json()
    assert set(body) == {
        "id",
        "organization_key",
        "display_name",
        "country_code",
        "timezone",
        "industry_code",
        "status",
        "version",
        "created_at",
        "updated_at",
    }
    assert "legal_name" not in body
    assert "tax_identifier" not in body


async def test_p01_endpoints_still_work(auth_client: Any) -> None:
    for path, expected in (
        ("/health/live", 200),
        ("/health/ready", 200),
        ("/version", 200),
        ("/api/v1/system/version", 200),
        (f"/api/v1/does-not-exist-{uuid.uuid4().hex}", 404),
    ):
        response = await auth_client.get(path)
        assert response.status_code == expected
