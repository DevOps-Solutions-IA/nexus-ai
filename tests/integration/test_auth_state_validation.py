"""Audit corrective matrix — live-state token validation (independent-audit Finding 1).

An access token that has ALREADY been issued must stop authorizing tenant access the
moment any server-side security state changes. Every test here uses the OLD token
against a real tenant endpoint (``GET /api/v1/organizations/current``) and demands a
fail-closed 401/403 — never 200 — plus explicit recovery semantics where applicable.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from nexus_ai.core.errors import SessionRevokedError
from nexus_ai.domain.auth.entities import Principal
from nexus_ai.domain.auth.rbac import RoleKey
from nexus_ai.domain.organizations.status import OrganizationStatus

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

PASSWORD = "correct-horse-battery-staple"


def _bearer(session: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['access_token']}"}


async def _org(session: dict[str, str], client: httpx.AsyncClient) -> httpx.Response:
    return await client.get("/api/v1/organizations/current", headers=_bearer(session))


async def _principal_for(resources: Any, login_helper: Any, email: str) -> Principal:
    session = await login_helper(email, PASSWORD)
    claims = resources.token_service.verify_access_token(session["access_token"])
    return Principal(
        user_id=claims.subject,
        session_id=claims.session_id,
        organization_id=claims.organization_id,
        token_id=claims.jti,
        issued_at=claims.issued_at,
        expires_at=claims.expires_at,
    )


async def _setup(
    auth_client: Any, make_auth_org: Any, make_auth_user: Any, login_helper: Any
) -> tuple[Any, str, Any, dict[str, str], Any]:
    org = await make_auth_org()
    email, _, user = await make_auth_user(organization=org, role=RoleKey.ORG_OWNER)
    session = await login_helper(email, PASSWORD)
    resources = auth_client.nexus_app.state.lifespan.resources  # type: ignore[attr-defined]
    return org, email, user, session, resources


class TestRevocationInvalidatesIssuedTokens:
    async def test_logout_revokes_session_and_old_token_is_rejected(
        self, auth_client: Any, make_auth_org: Any, make_auth_user: Any, login_helper: Any
    ) -> None:
        _the_org, _email, _user, session, _resources = await _setup(
            auth_client, make_auth_org, make_auth_user, login_helper
        )
        assert (await _org(session, auth_client)).status_code == 200  # sanity: works before
        logout = await auth_client.post(
            "/api/v1/auth/logout", json={"refresh_token": session["refresh_token"]}
        )
        assert logout.status_code == 204
        # The already-issued access token must now be unusable for tenant access.
        response = await _org(session, auth_client)
        assert response.status_code == 401
        assert response.json()["code"] == "NXS_AUTH_SESSION_REVOKED"

    async def test_membership_revocation_blocks_old_token(
        self, auth_client: Any, make_auth_org: Any, make_auth_user: Any, login_helper: Any
    ) -> None:
        org, email, user, session, resources = await _setup(
            auth_client, make_auth_org, make_auth_user, login_helper
        )
        assert (await _org(session, auth_client)).status_code == 200
        admin = await _principal_for(resources, login_helper, email)
        await resources.memberships.revoke(actor=admin, organization_id=org.id, user_id=user.id)
        response = await _org(session, auth_client)
        assert response.status_code == 403
        assert response.json()["code"] == "NXS_AUTH_MEMBERSHIP_INACTIVE"
        # /auth/me is equally blocked through the canonical boundary.
        me = await auth_client.get("/api/v1/auth/me", headers=_bearer(session))
        assert me.status_code == 403

    async def test_membership_suspension_blocks_old_token_and_restore_recovers(
        self, auth_client: Any, make_auth_org: Any, make_auth_user: Any, login_helper: Any
    ) -> None:
        org, email, user, session, resources = await _setup(
            auth_client, make_auth_org, make_auth_user, login_helper
        )
        admin = await _principal_for(resources, login_helper, email)
        await resources.memberships.suspend(actor=admin, organization_id=org.id, user_id=user.id)
        response = await _org(session, auth_client)
        assert response.status_code == 403
        assert response.json()["code"] == "NXS_AUTH_MEMBERSHIP_INACTIVE"
        # Recovery: restoring the membership makes the SAME token valid again.
        await resources.memberships.restore(actor=admin, organization_id=org.id, user_id=user.id)
        assert (await _org(session, auth_client)).status_code == 200

    async def test_user_suspension_blocks_old_token_and_reactivation_recovers(
        self, auth_client: Any, make_auth_org: Any, make_auth_user: Any, login_helper: Any
    ) -> None:
        _the_org, _email, user, session, resources = await _setup(
            auth_client, make_auth_org, make_auth_user, login_helper
        )
        from nexus_ai.domain.auth.repository import UserRepository

        async with resources.database.transaction() as db_session:
            await UserRepository(db_session).set_suspended(user.id, True)
        response = await _org(session, auth_client)
        assert response.status_code == 403
        assert response.json()["code"] == "NXS_AUTH_USER_INACTIVE"
        # Recovery: reactivating the user makes the same token valid again.
        async with resources.database.transaction() as db_session:
            await UserRepository(db_session).set_suspended(user.id, False)
        assert (await _org(session, auth_client)).status_code == 200

    async def test_organization_suspension_blocks_old_token_and_reactivation_recovers(
        self, auth_client: Any, make_auth_org: Any, make_auth_user: Any, login_helper: Any
    ) -> None:
        org, _email, _user, session, resources = await _setup(
            auth_client, make_auth_org, make_auth_user, login_helper
        )
        assert (await _org(session, auth_client)).status_code == 200
        await resources.organizations.transition(org.id, OrganizationStatus.SUSPENDED)
        response = await _org(session, auth_client)
        assert response.status_code == 403
        assert response.json()["code"] == "NXS_ORG_INACTIVE"
        # Recovery: reactivating the Organization makes the same token valid again.
        await resources.organizations.transition(org.id, OrganizationStatus.ACTIVE)
        assert (await _org(session, auth_client)).status_code == 200


class TestExpiredSessionRejected:
    async def test_expired_session_rejected_even_while_cryptographically_valid(
        self, auth_client: Any, make_auth_org: Any, make_auth_user: Any, login_helper: Any
    ) -> None:
        import datetime as dt

        from nexus_ai.domain.auth.state import PrincipalStateValidator

        _the_org, _email, _user, session, resources = await _setup(
            auth_client, make_auth_org, make_auth_user, login_helper
        )
        claims = resources.token_service.verify_access_token(session["access_token"])
        # A clock pushed past the refresh-session expiry: the token still verifies
        # cryptographically, but the session state is expired.
        past = dt.datetime.now(dt.UTC) + dt.timedelta(days=30)
        validator = PrincipalStateValidator(resources.database, now=lambda: past)
        with pytest.raises(SessionRevokedError):
            await validator.require_valid(
                user_id=claims.subject,
                session_id=claims.session_id,
                organization_id=claims.organization_id,
            )
