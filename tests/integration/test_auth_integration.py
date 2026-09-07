"""Authentication integration matrix (NXS-AUTH-001..009) against real PostgreSQL.

Login/refresh/logout flows, identity attacks (disabled user, suspended/revoked
membership, suspended/archived Organization, duplicate identity), tenant attacks
(valid user + foreign org hint, multi-org selection, body/header spoofing) and the
API-surface contracts (generic failures, no enumeration, no secret material in
responses) — all through the runtime role with forced RLS active.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from nexus_ai.core.errors import UserConflictError
from nexus_ai.domain.auth.entities import RegistrationDraft
from nexus_ai.domain.auth.rbac import RoleKey
from nexus_ai.domain.organizations.status import OrganizationStatus


def uuid_hex() -> str:
    return uuid.uuid4().hex[:8]


pytestmark = pytest.mark.anyio

PASSWORD = "correct-horse-battery-staple"


class TestLoginFlow:
    async def test_register_login_me_refresh_logout_cycle(
        self,
        auth_client: httpx.AsyncClient,
        make_auth_org: Any,
        make_auth_user: Any,
        login_helper: Any,
    ) -> None:
        org = await make_auth_org()
        email, _, _ = await make_auth_user(organization=org)

        session = await login_helper(email, PASSWORD)
        assert session["token_type"] == "bearer"
        assert session["organization_id"] == str(org.id)
        assert session["expires_in"] > 0

        # /me resolves the identity and the current session
        me = await auth_client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {session['access_token']}"}
        )
        assert me.status_code == 200
        assert me.json()["email"] == email
        assert me.json()["organization_id"] == str(org.id)

        # The bearer token resolves the tenant scope for tenant endpoints.
        org_response = await auth_client.get(
            "/api/v1/organizations/current",
            headers={"Authorization": f"Bearer {session['access_token']}"},
        )
        assert org_response.status_code == 200
        assert org_response.json()["id"] == str(org.id)

        # Refresh rotates the session.
        refreshed = await auth_client.post(
            "/api/v1/auth/refresh", json={"refresh_token": session["refresh_token"]}
        )
        assert refreshed.status_code == 200
        assert refreshed.json()["access_token"] != session["access_token"]
        assert refreshed.json()["refresh_token"] != session["refresh_token"]

        # The old refresh token is now replay — reuse revokes the family.
        replay = await auth_client.post(
            "/api/v1/auth/refresh", json={"refresh_token": session["refresh_token"]}
        )
        assert replay.status_code == 401
        after_replay = await auth_client.post(
            "/api/v1/auth/refresh", json={"refresh_token": refreshed.json()["refresh_token"]}
        )
        assert after_replay.status_code == 401

        # A fresh login establishes a new session; logout revokes it.
        second = await login_helper(email, PASSWORD)
        logout = await auth_client.post(
            "/api/v1/auth/logout", json={"refresh_token": second["refresh_token"]}
        )
        assert logout.status_code == 204
        revoked = await auth_client.post(
            "/api/v1/auth/refresh", json={"refresh_token": second["refresh_token"]}
        )
        assert revoked.status_code == 401
        # Logout is idempotent: a second logout of the same token also answers 204.
        assert (
            await auth_client.post(
                "/api/v1/auth/logout", json={"refresh_token": second["refresh_token"]}
            )
        ).status_code == 204

    async def test_email_normalization_unifies_login(
        self, make_auth_org: Any, make_auth_user: Any, login_helper: Any
    ) -> None:
        org = await make_auth_org()
        unique = f"Mixed.Case-{uuid_hex()}@Example.com"
        await make_auth_user(organization=org, email=unique)
        session = await login_helper(unique.lower(), PASSWORD)
        assert session["organization_id"] == str(org.id)

    async def test_duplicate_email_identity_rejected(
        self, auth_client: httpx.AsyncClient, make_auth_user: Any
    ) -> None:
        email, _, _ = await make_auth_user()
        resources = auth_client.nexus_app.state.lifespan.resources  # type: ignore[attr-defined]
        with pytest.raises(UserConflictError):
            await resources.auth.register_user(
                RegistrationDraft(email=email.upper(), password="another-password-12345")
            )


class TestIdentityAttacks:
    async def test_invalid_password_rejected_generically(
        self,
        make_auth_org: Any,
        make_auth_user: Any,
        login_helper: Any,
        auth_client: httpx.AsyncClient,
    ) -> None:
        org = await make_auth_org()
        email, _, _ = await make_auth_user(organization=org)
        response = await auth_client.post(
            "/api/v1/auth/login", json={"email": email, "password": "wrong-password-entirely"}
        )
        assert response.status_code == 401
        assert response.json()["code"] == "NXS_AUTH_CREDENTIALS_REJECTED"

    async def test_nonexistent_user_indistinguishable(
        self, make_auth_org: Any, make_auth_user: Any, auth_client: httpx.AsyncClient
    ) -> None:
        org = await make_auth_org()
        email, _, _ = await make_auth_user(organization=org)
        missing = await auth_client.post(
            "/api/v1/auth/login", json={"email": "ghost@example.com", "password": PASSWORD}
        )
        wrong = await auth_client.post(
            "/api/v1/auth/login", json={"email": email, "password": "wrong-password-entirely"}
        )
        assert missing.status_code == wrong.status_code == 401
        assert missing.json()["code"] == wrong.json()["code"]
        assert missing.json()["detail"] == wrong.json()["detail"]

    async def test_suspended_user_cannot_login(
        self,
        auth_client: httpx.AsyncClient,
        make_auth_org: Any,
        make_auth_user: Any,
    ) -> None:
        org = await make_auth_org()
        email, _, user = await make_auth_user(organization=org)
        resources = auth_client.nexus_app.state.lifespan.resources  # type: ignore[attr-defined]
        async with resources.database.transaction() as session:
            from nexus_ai.domain.auth.repository import UserRepository

            await UserRepository(session).set_suspended(user.id, True)
        response = await auth_client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        )
        assert response.status_code == 401
        assert response.json()["code"] == "NXS_AUTH_CREDENTIALS_REJECTED"

    async def test_suspended_membership_cannot_login(
        self,
        auth_client: httpx.AsyncClient,
        make_auth_org: Any,
        make_auth_user: Any,
        login_helper: Any,
    ) -> None:
        org = await make_auth_org()
        email, _, _ = await make_auth_user(organization=org)
        resources = auth_client.nexus_app.state.lifespan.resources  # type: ignore[attr-defined]
        actor_session = await login_helper(email, PASSWORD)
        claims = resources.token_service.verify_access_token(actor_session["access_token"])
        from nexus_ai.domain.auth.entities import Principal

        actor = Principal(
            user_id=claims.subject,
            session_id=claims.session_id,
            organization_id=claims.organization_id,
            token_id=claims.jti,
            issued_at=claims.issued_at,
            expires_at=claims.expires_at,
        )
        user_row = None
        async with resources.database.transaction() as session:
            from nexus_ai.domain.auth.repository import UserRepository

            user_row = await UserRepository(session).by_email(email)
        assert user_row is not None
        await resources.memberships.suspend(
            actor=actor, organization_id=org.id, user_id=user_row.id
        )
        response = await auth_client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        )
        assert response.status_code == 403
        assert response.json()["code"] == "NXS_AUTH_MEMBERSHIP_REQUIRED"

    async def test_suspended_organization_rejects_login(
        self,
        auth_client: httpx.AsyncClient,
        make_auth_org: Any,
        make_auth_user: Any,
    ) -> None:
        org = await make_auth_org()
        email, _, _ = await make_auth_user(organization=org)
        resources = auth_client.nexus_app.state.lifespan.resources  # type: ignore[attr-defined]
        await resources.organizations.transition(org.id, OrganizationStatus.SUSPENDED)
        response = await auth_client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        )
        assert response.status_code == 403
        assert response.json()["code"] == "NXS_ORG_INACTIVE"


class TestTenantAttacks:
    async def test_foreign_org_hint_rejected_generically(
        self,
        auth_client: httpx.AsyncClient,
        make_auth_org: Any,
        make_auth_user: Any,
    ) -> None:
        org = await make_auth_org()
        foreign = await make_auth_org()
        email, _, _ = await make_auth_user(organization=org)
        # Valid user A + Organization B identifier: same generic failure as bad creds.
        response = await auth_client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": PASSWORD, "organization_id": str(foreign.id)},
        )
        assert response.status_code == 401
        assert response.json()["code"] == "NXS_AUTH_CREDENTIALS_REJECTED"

    async def test_unknown_org_hint_rejected_generically(
        self, auth_client: httpx.AsyncClient, make_auth_org: Any, make_auth_user: Any
    ) -> None:
        import uuid

        org = await make_auth_org()
        email, _, _ = await make_auth_user(organization=org)
        response = await auth_client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": PASSWORD, "organization_id": str(uuid.uuid7())},
        )
        assert response.status_code == 401
        assert response.json()["code"] == "NXS_AUTH_CREDENTIALS_REJECTED"

    async def test_spoofed_org_body_field_is_ignored(
        self, auth_client: httpx.AsyncClient, make_auth_org: Any, make_auth_user: Any
    ) -> None:
        org = await make_auth_org()
        email, _, _ = await make_auth_user(organization=org)
        # The login payload schema forbids unknown fields; even a smuggled
        # "X-Organization-ID" body key cannot steer scope.
        response = await auth_client.post(
            "/api/v1/auth/login",
            json={
                "email": email,
                "password": PASSWORD,
                "X-NXS-Organization-ID": str(org.id),
            },
        )
        assert response.status_code == 422  # extra fields forbidden by the schema

    async def test_multi_org_login_requires_explicit_selection(
        self,
        auth_client: httpx.AsyncClient,
        make_auth_org: Any,
        make_auth_user: Any,
        login_helper: Any,
    ) -> None:
        first = await make_auth_org()
        second = await make_auth_org()
        email, _, user = await make_auth_user(organization=first)
        resources = auth_client.nexus_app.state.lifespan.resources  # type: ignore[attr-defined]
        await resources.memberships.create(
            actor=None, organization_id=second.id, user_id=user.id, role=RoleKey.ORG_MEMBER
        )
        ambiguous = await auth_client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        )
        assert ambiguous.status_code == 400
        assert ambiguous.json()["code"] == "NXS_AUTH_MULTIPLE_ORGS"
        offered = {entry["organization_id"] for entry in ambiguous.json()["organizations"]}
        assert offered == {str(first.id), str(second.id)}  # only the caller's own orgs
        # Explicit selection then works for either membership.
        for target in (first, second):
            session = await login_helper(email, PASSWORD, organization_id=target.id)
            assert session["organization_id"] == str(target.id)

    async def test_membership_listing_shows_only_own_organizations(
        self,
        auth_client: httpx.AsyncClient,
        make_auth_org: Any,
        make_auth_user: Any,
        login_helper: Any,
    ) -> None:
        mine = await make_auth_org()
        await make_auth_org()
        email, _, _user = await make_auth_user(organization=mine)
        # A second user in a second org must never appear in my listing.
        foreign = await make_auth_org()
        await make_auth_user(organization=foreign)
        session = await login_helper(email, PASSWORD)
        response = await auth_client.get(
            "/api/v1/auth/memberships",
            headers={"Authorization": f"Bearer {session['access_token']}"},
        )
        assert response.status_code == 200
        assert [entry["organization_id"] for entry in response.json()] == [str(mine.id)]


class TestRateLimiting:
    async def test_login_failures_throttled_per_identity(
        self, auth_client: httpx.AsyncClient, make_auth_org: Any, make_auth_user: Any
    ) -> None:
        org = await make_auth_org()
        email, _, _ = await make_auth_user(organization=org)
        for _ in range(5):
            response = await auth_client.post(
                "/api/v1/auth/login",
                json={"email": email, "password": "wrong-password-entirely"},
            )
            assert response.status_code == 401
        throttled = await auth_client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "wrong-password-entirely"},
        )
        assert throttled.status_code == 429
        assert throttled.json()["code"] == "NXS_CORE_RATE_LIMITED"
        # A different identity from the same host is NOT collateral.
        other = await make_auth_user(organization=org)
        other_response = await auth_client.post(
            "/api/v1/auth/login", json={"email": other[0], "password": PASSWORD}
        )
        assert other_response.status_code == 200

    async def test_successful_login_resets_the_failure_counter(
        self, auth_client: httpx.AsyncClient, make_auth_org: Any, make_auth_user: Any
    ) -> None:
        org = await make_auth_org()
        email, _, _ = await make_auth_user(organization=org)
        for _ in range(4):
            await auth_client.post(
                "/api/v1/auth/login", json={"email": email, "password": "wrong-password-entirely"}
            )
        success = await auth_client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        )
        assert success.status_code == 200
        # Four more failures are allowed again (counter was reset).
        for _ in range(4):
            response = await auth_client.post(
                "/api/v1/auth/login", json={"email": email, "password": "wrong-password-entirely"}
            )
            assert response.status_code == 401


class TestSecretLeakageApi:
    async def test_responses_carry_no_secret_material(
        self,
        auth_client: httpx.AsyncClient,
        make_auth_org: Any,
        make_auth_user: Any,
        login_helper: Any,
    ) -> None:
        org = await make_auth_org()
        email, _, _ = await make_auth_user(organization=org)
        session = await login_helper(email, PASSWORD)
        raw = str(session)
        assert PASSWORD not in raw
        assert "$argon2" not in raw
        # The refresh token appears exactly once — in the response that issued it.
        assert raw.count(session["refresh_token"]) == 1

        me = await auth_client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {session['access_token']}"}
        )
        assert PASSWORD not in me.text
        assert session["refresh_token"] not in me.text

    async def test_error_responses_carry_no_stack_or_sql(
        self, auth_client: httpx.AsyncClient, make_auth_org: Any, make_auth_user: Any
    ) -> None:
        org = await make_auth_org()
        email, _, _ = await make_auth_user(organization=org)
        response = await auth_client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "wrong-password-entirely"},
        )
        for forbidden in ("Traceback", "SELECT", "INSERT", "asyncpg", "sqlalchemy", PASSWORD):
            assert forbidden not in response.text
