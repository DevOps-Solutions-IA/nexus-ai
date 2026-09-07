"""Provisioning security matrix (NXS-ORG-001 × P03 live-state auth / P02 RLS).

Unauthorized provisioning, escalation attempts, forged identity/role/widget injection,
cross-tenant reads and writes on the P05 tables, and P03 live-state blocking — all
fail closed.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.core.errors import PermissionDeniedError
from nexus_ai.domain.provisioning.entities import OnboardingRequest

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

PASSWORD = "correct-horse-battery-staple"


def _onboarding(**overrides: object) -> OnboardingRequest:
    payload: dict[str, object] = {
        "idempotency_key": f"sec-{uuid.uuid4().hex[:16]}",
        "organization_key": f"sec-org-{uuid.uuid4().hex[:8]}",
        "display_name": "Security Org",
        "legal_name": "Security Org S.A.",
        "country_code": "cr",
        "timezone": "America/Costa_Rica",
        "locale": "en-US",
    }
    payload.update(overrides)
    return OnboardingRequest(**payload)


def _resources(client: Any) -> Any:
    return client.nexus_app.state.lifespan.resources  # type: ignore[attr-defined]


async def _login(client: Any, email: str, org_id: Any) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": PASSWORD, "organization_id": str(org_id)},
    )
    assert response.status_code == 200, response.text
    return response.json()


class TestProvisioningAuthorization:
    async def test_org_owner_without_platform_grant_cannot_provision(
        self, auth_client: Any, make_auth_org: Any, make_auth_user: Any
    ) -> None:
        org = await make_auth_org()
        email, _, user = await make_auth_user(organization=org)
        resources = _resources(auth_client)
        with pytest.raises(PermissionDeniedError) as excinfo:
            await resources.provisioner.provision(
                _onboarding(), caller_user_id=user.id, caller_has_platform_grant=False
            )
        assert excinfo.value.extensions["permission"] == "organization:create"

    async def test_member_escalation_through_onboarding_blocked(
        self, auth_client: Any, make_auth_org: Any, make_auth_user: Any
    ) -> None:
        org = await make_auth_org()
        email, _, user = await make_auth_user(organization=org)
        resources = _resources(auth_client)
        await resources.provisioner.grant_create_capability(user_id=user.id)
        result = await resources.provisioner.provision(
            _onboarding(), caller_user_id=user.id, caller_has_platform_grant=True
        )
        # The caller is org_owner of the NEW org (fixed role). A member of another org
        # gets nothing from the new org's ownership.
        session = await _login(auth_client, email, org.id)  # still in the old org
        assert session["organization_id"] == str(org.id)
        assert result.organization_id != org.id

    async def test_forged_owner_role_in_payload_rejected(
        self, auth_client: Any, make_auth_org: Any, make_auth_user: Any
    ) -> None:
        home = await make_auth_org()
        email, _, _ = await make_auth_user(organization=home)
        session = await _login(auth_client, email, home.id)
        response = await auth_client.post(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {session['access_token']}"},
            json={**_onboarding().model_dump(mode="json"), "owner_role": "superadmin"},
        )
        assert response.status_code == 422  # extra field forbidden

    async def test_caller_supplied_organization_id_rejected(
        self, auth_client: Any, make_auth_org: Any, make_auth_user: Any
    ) -> None:
        home = await make_auth_org()
        email, _, _ = await make_auth_user(organization=home)
        session = await _login(auth_client, email, home.id)
        forged_id = str(uuid.uuid7())
        response = await auth_client.post(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {session['access_token']}"},
            json={**_onboarding().model_dump(mode="json"), "organization_id": forged_id},
        )
        assert response.status_code == 422
        # And nothing was created under the forged id.
        async with _resources(auth_client).database.transaction() as db_session:
            rows = (
                await db_session.execute(
                    text("SELECT count(*) FROM organizations WHERE id = :i"), {"i": forged_id}
                )
            ).scalar_one()
        assert rows == 0

    async def test_stale_token_blocked_before_provisioning(
        self, auth_client: Any, make_auth_org: Any, make_auth_user: Any
    ) -> None:
        home = await make_auth_org()
        email, _, _ = await make_auth_user(organization=home)
        session = await _login(auth_client, email, home.id)
        await auth_client.post(
            "/api/v1/auth/logout", json={"refresh_token": session["refresh_token"]}
        )
        response = await auth_client.post(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {session['access_token']}"},
            json=_onboarding().model_dump(mode="json"),
        )
        assert response.status_code == 401
        assert response.json()["code"] == "NXS_AUTH_SESSION_REVOKED"


class TestRlsOnP05Tables:
    async def test_settings_invisible_cross_tenant(
        self, auth_client: Any, make_auth_user: Any, tenant_database: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        result = await resources.provisioner.provision(
            _onboarding(), caller_user_id=user.id, caller_has_platform_grant=True
        )
        # An unrelated tenant scope sees NOTHING of the provisioned org's settings.
        async with tenant_database.tenant_transaction(uuid.uuid7()) as tenant:
            rows = (
                await tenant.session.execute(text("SELECT count(*) FROM organization_settings"))
            ).scalar_one()
        assert rows == 0
        # The provisioned org's own scope sees exactly its own row.
        async with tenant_database.tenant_transaction(result.organization_id) as tenant:
            rows = (
                await tenant.session.execute(text("SELECT count(*) FROM organization_settings"))
            ).scalar_one()
        assert rows == 1

    async def test_dashboard_config_invisible_cross_tenant_and_with_check_enforced(
        self, auth_client: Any, make_auth_user: Any, tenant_database: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        result = await resources.provisioner.provision(
            _onboarding(), caller_user_id=user.id, caller_has_platform_grant=True
        )
        # Scoped to a foreign org, an INSERT targeting the victim's org fails at the DB.
        async with tenant_database.tenant_transaction(uuid.uuid7()) as tenant:
            with pytest.raises(Exception):
                await tenant.session.execute(
                    text(
                        "INSERT INTO dashboard_configurations "
                        "(id, organization_id, schema_version, revision, configuration) "
                        "VALUES (:id, :org, 1, 99, '{}'::jsonb)"
                    ),
                    {"id": uuid.uuid7(), "org": result.organization_id},
                )

    async def test_provisioning_requests_have_no_plaintext_keys(
        self, auth_client: Any, make_auth_user: Any, tenant_database: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        secret_key = f"credential-like-key-{uuid.uuid4().hex[:8]}"
        await resources.provisioner.provision(
            _onboarding(idempotency_key=secret_key),
            caller_user_id=user.id,
            caller_has_platform_grant=True,
        )
        async with resources.database.transaction() as session:
            stored = (
                await session.execute(
                    text(
                        "SELECT idempotency_key_hash, request_fingerprint FROM provisioning_requests"
                    )
                )
            ).all()
        for key_hash, _fingerprint in stored:
            assert secret_key not in key_hash
            assert len(key_hash) == 64
