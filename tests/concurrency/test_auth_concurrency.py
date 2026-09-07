"""Authentication concurrency and session-safety matrix (NXS-AUTH-004, NXS-AUTH-007).

Refresh rotation races, logout-vs-refresh races, simultaneous logins, suspension vs
active sessions and multi-tenant concurrent request isolation — every outcome must be
deterministic, with the tenant ContextVar never leaking between tasks.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from nexus_ai.domain.auth.rbac import RoleKey

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

PASSWORD = "correct-horse-battery-staple"


class TestRefreshRaces:
    async def test_concurrent_refresh_with_same_token_exactly_one_wins(
        self,
        auth_client: httpx.AsyncClient,
        make_auth_org: Any,
        make_auth_user: Any,
        login_helper: Any,
    ) -> None:
        org = await make_auth_org()
        email, _, _ = await make_auth_user(organization=org)
        session = await login_helper(email, PASSWORD)

        async def _refresh() -> int:
            response = await auth_client.post(
                "/api/v1/auth/refresh", json={"refresh_token": session["refresh_token"]}
            )
            return response.status_code

        results = await asyncio.gather(*[_refresh() for _ in range(8)])
        # Deterministic outcome: exactly one caller wins the rotation.
        assert results.count(200) == 1
        assert results.count(401) == 7

    async def test_replay_after_rotation_revokes_family_for_all_concurrent_users(
        self,
        auth_client: httpx.AsyncClient,
        make_auth_org: Any,
        make_auth_user: Any,
        login_helper: Any,
    ) -> None:
        org = await make_auth_org()
        email, _, _ = await make_auth_user(organization=org)
        session = await login_helper(email, PASSWORD)
        refreshed = await auth_client.post(
            "/api/v1/auth/refresh", json={"refresh_token": session["refresh_token"]}
        )
        new_token = refreshed.json()["refresh_token"]
        # Whoever holds the OLD token after rotation is a replay: family revoked, and
        # the NEW token dies with it — deterministic for every holder.
        replay = await auth_client.post(
            "/api/v1/auth/refresh", json={"refresh_token": session["refresh_token"]}
        )
        assert replay.status_code == 401
        after = await auth_client.post("/api/v1/auth/refresh", json={"refresh_token": new_token})
        assert after.status_code == 401

    async def test_logout_vs_refresh_race_is_deterministic(
        self,
        auth_client: httpx.AsyncClient,
        make_auth_org: Any,
        make_auth_user: Any,
        login_helper: Any,
    ) -> None:
        org = await make_auth_org()
        email, _, _ = await make_auth_user(organization=org)
        for _ in range(5):
            session = await login_helper(email, PASSWORD)
            refresh = auth_client.post(
                "/api/v1/auth/refresh", json={"refresh_token": session["refresh_token"]}
            )
            logout = auth_client.post(
                "/api/v1/auth/logout", json={"refresh_token": session["refresh_token"]}
            )
            refresh_response, logout_response = await asyncio.gather(refresh, logout)
            assert logout_response.status_code == 204
            # Refresh either won the race (200) or lost to revocation (401) — never
            # anything else, and never a half-rotated session.
            assert refresh_response.status_code in {200, 401}
            if refresh_response.status_code == 200:
                subsequent = await auth_client.post(
                    "/api/v1/auth/refresh",
                    json={"refresh_token": refresh_response.json()["refresh_token"]},
                )
                assert subsequent.status_code == 401  # logout still revoked the family


class TestLoginConcurrency:
    async def test_simultaneous_logins_yield_independent_sessions(
        self,
        auth_client: httpx.AsyncClient,
        make_auth_org: Any,
        make_auth_user: Any,
        login_helper: Any,
    ) -> None:
        org = await make_auth_org()
        email, _, _ = await make_auth_user(organization=org)

        async def _login() -> dict[str, str]:
            response = await auth_client.post(
                "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
            )
            assert response.status_code == 200
            return response.json()

        sessions = await asyncio.gather(*[_login() for _ in range(6)])
        session_ids = {s["session_id"] for s in sessions}
        refresh_tokens = {s["refresh_token"] for s in sessions}
        assert len(session_ids) == 6  # six independent server-side sessions
        assert len(refresh_tokens) == 6  # no token collision
        # Every access token verifies and carries the right org.
        resources = _resources(auth_client)
        for s in sessions:
            claims = resources.token_service.verify_access_token(s["access_token"])
            assert str(claims.organization_id) == str(org.id)
        # Logging out any one session leaves the others alive.
        await auth_client.post(
            "/api/v1/auth/logout", json={"refresh_token": sessions[0]["refresh_token"]}
        )
        for s in sessions[1:]:
            me = await auth_client.get(
                "/api/v1/auth/me", headers={"Authorization": f"Bearer {s['access_token']}"}
            )
            assert me.status_code == 200


class TestSuspensionVsSessions:
    async def test_membership_revocation_blocks_refresh_and_tenant_access(
        self,
        auth_client: httpx.AsyncClient,
        make_auth_org: Any,
        make_auth_user: Any,
        login_helper: Any,
    ) -> None:
        org = await make_auth_org()
        admin_email, _, _ = await make_auth_user(organization=org, role=RoleKey.ORG_OWNER)
        member_email, _, member = await make_auth_user(organization=org, role=RoleKey.ORG_MEMBER)
        resources = _resources(auth_client)
        session = await login_helper(member_email, PASSWORD)
        admin_session = await login_helper(admin_email, PASSWORD)
        admin_claims = resources.token_service.verify_access_token(admin_session["access_token"])
        from nexus_ai.domain.auth.entities import Principal

        admin = Principal(
            user_id=admin_claims.subject,
            session_id=admin_claims.session_id,
            organization_id=admin_claims.organization_id,
            token_id=admin_claims.jti,
            issued_at=admin_claims.issued_at,
            expires_at=admin_claims.expires_at,
        )
        await resources.memberships.revoke(actor=admin, organization_id=org.id, user_id=member.id)
        # Refresh now fails: the session cannot re-validate membership.
        refresh = await auth_client.post(
            "/api/v1/auth/refresh", json={"refresh_token": session["refresh_token"]}
        )
        assert refresh.status_code == 401
        # The access token still verifies cryptographically but tenant access is gone:
        # re-login is refused outright.
        relogin = await auth_client.post(
            "/api/v1/auth/login", json={"email": member_email, "password": PASSWORD}
        )
        assert relogin.status_code == 403

    async def test_user_suspension_blocks_refresh(
        self,
        auth_client: httpx.AsyncClient,
        make_auth_org: Any,
        make_auth_user: Any,
        login_helper: Any,
    ) -> None:
        org = await make_auth_org()
        email, _, user = await make_auth_user(organization=org)
        resources = _resources(auth_client)
        session = await login_helper(email, PASSWORD)
        async with resources.database.transaction() as db_session:
            from nexus_ai.domain.auth.repository import UserRepository

            await UserRepository(db_session).set_suspended(user.id, True)
        refresh = await auth_client.post(
            "/api/v1/auth/refresh", json={"refresh_token": session["refresh_token"]}
        )
        assert refresh.status_code == 401
        # The suspension also revoked the session server-side: the token is dead even
        # if the user is later reinstated.
        async with resources.database.transaction() as db_session:
            await UserRepository(db_session).set_suspended(user.id, False)
        again = await auth_client.post(
            "/api/v1/auth/refresh", json={"refresh_token": session["refresh_token"]}
        )
        assert again.status_code == 401


class TestMultiTenantConcurrency:
    async def test_concurrent_requests_from_different_organizations_stay_isolated(
        self,
        auth_client: httpx.AsyncClient,
        make_auth_org: Any,
        make_auth_user: Any,
        login_helper: Any,
    ) -> None:
        org_a = await make_auth_org()
        org_b = await make_auth_org()
        email_a, _, _ = await make_auth_user(organization=org_a)
        email_b, _, _ = await make_auth_user(organization=org_b)
        session_a = await login_helper(email_a, PASSWORD)
        session_b = await login_helper(email_b, PASSWORD)

        async def _read_org(session: dict[str, str], expected: str) -> str:
            for _ in range(3):
                response = await auth_client.get(
                    "/api/v1/organizations/current",
                    headers={"Authorization": f"Bearer {session['access_token']}"},
                )
                assert response.status_code == 200
                assert response.json()["id"] == expected
            return expected

        results = await asyncio.gather(
            *[
                _read_org(session_a, str(org_a.id)),
                _read_org(session_b, str(org_b.id)),
                _read_org(session_a, str(org_a.id)),
                _read_org(session_b, str(org_b.id)),
            ]
        )
        assert results.count(str(org_a.id)) == 2
        assert results.count(str(org_b.id)) == 2

    async def test_me_endpoint_never_leaks_foreign_org_context(
        self,
        auth_client: httpx.AsyncClient,
        make_auth_org: Any,
        make_auth_user: Any,
        login_helper: Any,
    ) -> None:
        org_a = await make_auth_org()
        org_b = await make_auth_org()
        email_a, _, _ = await make_auth_user(organization=org_a)
        email_b, _, _ = await make_auth_user(organization=org_b)
        session_a = await login_helper(email_a, PASSWORD)
        session_b = await login_helper(email_b, PASSWORD)

        async def _me(session: dict[str, str], expected_org: str) -> None:
            for _ in range(5):
                response = await auth_client.get(
                    "/api/v1/auth/me",
                    headers={"Authorization": f"Bearer {session['access_token']}"},
                )
                assert response.status_code == 200
                assert response.json()["organization_id"] == expected_org

        await asyncio.gather(
            _me(session_a, str(org_a.id)),
            _me(session_b, str(org_b.id)),
            _me(session_a, str(org_a.id)),
            _me(session_b, str(org_b.id)),
        )


def _resources(client: httpx.AsyncClient) -> Any:
    return client.nexus_app.state.lifespan.resources  # type: ignore[attr-defined]
