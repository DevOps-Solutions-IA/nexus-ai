"""RBAC enforcement on the Organization API (independent-audit Finding 2).

``GET /api/v1/organizations/current`` requires ``organization:read``;
``PATCH`` requires ``organization:write`` — resolved through the canonical
``AuthorizationService`` over tenant-scoped assignments, deny-by-default.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from nexus_ai.domain.auth.entities import Principal
from nexus_ai.domain.auth.rbac import RoleKey

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

PASSWORD = "correct-horse-battery-staple"


def _bearer(session: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['access_token']}"}


async def _login(auth_client: Any, email: str, org_id: Any | None = None) -> dict[str, str]:
    payload: dict[str, Any] = {"email": email, "password": PASSWORD}
    if org_id is not None:
        payload["organization_id"] = str(org_id)
    response = await auth_client.post("/api/v1/auth/login", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


async def _patch(
    client: httpx.AsyncClient, session: dict[str, str], version: int
) -> httpx.Response:
    return await client.patch(
        "/api/v1/organizations/current",
        headers=_bearer(session),
        json={"display_name": "Renamed", "expected_version": version},
    )


async def _principal(resources: Any, session: dict[str, str]) -> Principal:
    claims = resources.token_service.verify_access_token(session["access_token"])
    return Principal(
        user_id=claims.subject,
        session_id=claims.session_id,
        organization_id=claims.organization_id,
        token_id=claims.jti,
        issued_at=claims.issued_at,
        expires_at=claims.expires_at,
    )


class TestOrganizationEndpointRbac:
    async def test_org_owner_patch_allowed(
        self, auth_client: Any, make_auth_org: Any, make_auth_user: Any
    ) -> None:
        org = await make_auth_org()
        email, _, _ = await make_auth_user(organization=org, role=RoleKey.ORG_OWNER)
        session = await _login(auth_client, email)
        response = await _patch(auth_client, session, org.version)
        assert response.status_code == 200
        assert response.json()["display_name"] == "Renamed"

    async def test_org_admin_patch_allowed(
        self, auth_client: Any, make_auth_org: Any, make_auth_user: Any
    ) -> None:
        org = await make_auth_org()
        email, _, _ = await make_auth_user(organization=org, role=RoleKey.ORG_ADMIN)
        session = await _login(auth_client, email)
        response = await _patch(auth_client, session, org.version)
        assert response.status_code == 200

    async def test_org_member_patch_denied(
        self, auth_client: Any, make_auth_org: Any, make_auth_user: Any
    ) -> None:
        org = await make_auth_org()
        email, _, _ = await make_auth_user(organization=org, role=RoleKey.ORG_MEMBER)
        session = await _login(auth_client, email)
        response = await _patch(auth_client, session, org.version)
        assert response.status_code == 403
        assert response.json()["code"] == "NXS_CORE_PERMISSION_DENIED"
        assert response.json()["permission"] == "organization:write"

    async def test_org_member_get_allowed(
        self, auth_client: Any, make_auth_org: Any, make_auth_user: Any
    ) -> None:
        org = await make_auth_org()
        email, _, _ = await make_auth_user(organization=org, role=RoleKey.ORG_MEMBER)
        session = await _login(auth_client, email)
        response = await auth_client.get("/api/v1/organizations/current", headers=_bearer(session))
        assert response.status_code == 200
        assert response.json()["id"] == str(org.id)

    async def test_role_from_another_organization_cannot_authorize_here(
        self, auth_client: Any, make_auth_org: Any, make_auth_user: Any
    ) -> None:
        org_a = await make_auth_org()
        org_b = await make_auth_org()
        email, _, user = await make_auth_user(organization=org_a, role=RoleKey.ORG_OWNER)
        resources = auth_client.nexus_app.state.lifespan.resources  # type: ignore[attr-defined]
        await resources.memberships.create(
            actor=None, organization_id=org_b.id, user_id=user.id, role=RoleKey.ORG_MEMBER
        )
        # Logged into org B: the org-A ownership grants NOTHING here.
        session = await _login(auth_client, email, org_id=org_b.id)
        response = await _patch(auth_client, session, org_b.version)
        assert response.status_code == 403
        assert response.json()["code"] == "NXS_CORE_PERMISSION_DENIED"

    async def test_suspended_assignment_denied(
        self, auth_client: Any, make_auth_org: Any, make_auth_user: Any
    ) -> None:
        org = await make_auth_org()
        admin_email, _, _ = await make_auth_user(organization=org, role=RoleKey.ORG_OWNER)
        member_email, _, member = await make_auth_user(organization=org, role=RoleKey.ORG_ADMIN)
        resources = auth_client.nexus_app.state.lifespan.resources  # type: ignore[attr-defined]
        admin_session = await _login(auth_client, admin_email)
        admin = await _principal(resources, admin_session)
        from sqlalchemy import text

        async with resources.database.tenant_transaction(org.id) as tenant:
            assignment_id = (
                (
                    await tenant.session.execute(
                        text(
                            "SELECT ra.id FROM role_assignments ra "
                            "JOIN roles r ON r.id = ra.role_id "
                            "WHERE ra.user_id = :u AND r.role_key = 'org_admin'"
                        ),
                        {"u": member.id},
                    )
                )
                .scalars()
                .one()
            )
        await resources.memberships.suspend_assignment(
            actor=admin, organization_id=org.id, assignment_id=assignment_id
        )
        member_session = await _login(auth_client, member_email)
        response = await _patch(auth_client, member_session, org.version)
        assert response.status_code == 403
        assert response.json()["code"] == "NXS_CORE_PERMISSION_DENIED"

    async def test_revoked_session_denied_before_rbac(
        self, auth_client: Any, make_auth_org: Any, make_auth_user: Any
    ) -> None:
        org = await make_auth_org()
        email, _, _ = await make_auth_user(organization=org, role=RoleKey.ORG_OWNER)
        session = await _login(auth_client, email)
        assert (await _patch(auth_client, session, org.version)).status_code == 200
        logout = await auth_client.post(
            "/api/v1/auth/logout", json={"refresh_token": session["refresh_token"]}
        )
        assert logout.status_code == 204
        # The stale token must be denied at the state boundary (401), never reach RBAC.
        response = await _patch(auth_client, session, org.version + 1)
        assert response.status_code == 401
        assert response.json()["code"] == "NXS_AUTH_SESSION_REVOKED"

    async def test_unauthenticated_patch_denied(self, auth_client: Any, make_auth_org: Any) -> None:
        org = await make_auth_org()
        response = await auth_client.patch(
            "/api/v1/organizations/current",
            json={"display_name": "Hijacked", "expected_version": org.version},
        )
        assert response.status_code == 403
        assert response.json()["code"] == "NXS_TENANT_CONTEXT_REQUIRED"
