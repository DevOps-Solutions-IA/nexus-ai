"""Organization provisioning matrix (NXS-ORG-001) against real PostgreSQL.

Success, owner binding, settings, dashboard baseline, transactional outbox intent,
atomic rollback, idempotent replay, altered-payload conflict, unique-slug conflict,
terminal-failure replay, crash-resume semantics and the status projection.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from nexus_ai.core.errors import (
    IdempotencyKeyConflictError,
    OrganizationConflictError,
    ProvisioningFailedError,
    ProvisioningInProgressError,
)
from nexus_ai.domain.provisioning.entities import OnboardingRequest, ProvisioningResult

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

PASSWORD = "correct-horse-battery-staple"


def _onboarding(**overrides: object) -> OnboardingRequest:
    payload: dict[str, object] = {
        "idempotency_key": f"onboard-{uuid.uuid4().hex[:16]}",
        "organization_key": f"prov-org-{uuid.uuid4().hex[:8]}",
        "display_name": "Provisioned Org",
        "legal_name": "Provisioned Org S.A.",
        "country_code": "cr",
        "timezone": "America/Costa_Rica",
        "locale": "en-US",
    }
    payload.update(overrides)
    return OnboardingRequest(**payload)


def _resources(client: httpx.AsyncClient) -> Any:
    return client.nexus_app.state.lifespan.resources  # type: ignore[attr-defined]


class TestProvisioningSuccess:
    async def test_provision_creates_a_fully_operational_organization(
        self, auth_client: Any, make_auth_user: Any
    ) -> None:
        resources = _resources(auth_client)
        email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)

        request = _onboarding()
        result = await resources.provisioner.provision(
            request, caller_user_id=user.id, caller_has_platform_grant=True
        )
        assert isinstance(result, ProvisioningResult)
        assert result.organization_status == "ACTIVE"
        assert result.provisioning_status.value == "COMPLETED"

        # The Organization resolves through the P02/P03 system immediately: the owner
        # can log in, read the profile and the new surfaces.
        session = await _login(auth_client, email, result.organization_id)
        profile = await auth_client.get(
            "/api/v1/organizations/current",
            headers={"Authorization": f"Bearer {session['access_token']}"},
        )
        assert profile.status_code == 200
        assert profile.json()["id"] == str(result.organization_id)

        status = await auth_client.get(
            "/api/v1/organizations/current/provisioning",
            headers={"Authorization": f"Bearer {session['access_token']}"},
        )
        assert status.status_code == 200
        assert status.json()["status"] == "COMPLETED"

        settings = await auth_client.get(
            "/api/v1/organizations/current/settings",
            headers={"Authorization": f"Bearer {session['access_token']}"},
        )
        assert settings.status_code == 200
        assert settings.json()["locale"] == "en-US"

        dashboard = await auth_client.get(
            "/api/v1/organizations/current/dashboard-schema",
            headers={"Authorization": f"Bearer {session['access_token']}"},
        )
        assert dashboard.status_code == 200
        body = dashboard.json()
        assert body["schema_version"] == 1
        assert body["organization_id"] == str(result.organization_id)
        widget_keys = {w["widget_key"] for s in body["sections"] for w in s["widgets"]}
        assert widget_keys == {
            "organization.profile",
            "organization.provisioning_status",
            "organization.settings_summary",
        }

    async def test_owner_binding_is_org_owner_with_fixed_role(
        self, auth_client: Any, make_auth_user: Any, tenant_database: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        result = await resources.provisioner.provision(
            _onboarding(), caller_user_id=user.id, caller_has_platform_grant=True
        )
        async with tenant_database.tenant_transaction(result.organization_id) as tenant:
            rows = (
                await tenant.session.execute(
                    text(
                        "SELECT r.role_key FROM role_assignments ra "
                        "JOIN roles r ON r.id = ra.role_id WHERE ra.user_id = :u"
                    ),
                    {"u": user.id},
                )
            ).scalars()
        assert list(rows) == ["org_owner"]

    async def test_explicit_existing_owner_supported(
        self, auth_client: Any, make_auth_user: Any
    ) -> None:
        resources = _resources(auth_client)
        _operator_email, _, operator = await make_auth_user()
        _owner_email, _, owner = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=operator.id)
        result = await resources.provisioner.provision(
            _onboarding(owner_user_id=owner.id),
            caller_user_id=operator.id,
            caller_has_platform_grant=True,
        )
        session = await _login(auth_client, _owner_email, result.organization_id)
        me = await auth_client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {session['access_token']}"}
        )
        assert me.status_code == 200

    async def test_outbox_intent_commits_with_the_provisioning_transaction(
        self, auth_client: Any, make_auth_user: Any, tenant_database: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        result = await resources.provisioner.provision(
            _onboarding(), caller_user_id=user.id, caller_has_platform_grant=True
        )
        async with tenant_database.tenant_transaction(result.organization_id) as tenant:
            rows = (
                await tenant.session.execute(
                    text(
                        "SELECT event_type, subject, status FROM event_outbox "
                        "WHERE event_type = 'organizations.provisioned'"
                    )
                )
            ).all()
        assert len(rows) == 1
        assert rows[0][1] == "nxs.test.tenant.organizations.provisioned"


class TestIdempotency:
    async def test_same_key_same_payload_replays_the_same_result(
        self, auth_client: Any, make_auth_user: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        request = _onboarding()
        first = await resources.provisioner.provision(
            request, caller_user_id=user.id, caller_has_platform_grant=True
        )
        replay = await resources.provisioner.provision(
            request, caller_user_id=user.id, caller_has_platform_grant=True
        )
        assert replay.organization_id == first.organization_id
        # Exactly ONE Organization exists for the idempotent request (counted inside
        # its own tenant scope — RLS hides it from any unscoped query).
        async with resources.database.tenant_transaction(first.organization_id) as tenant:
            count = (
                await tenant.session.execute(
                    text("SELECT count(*) FROM organizations WHERE id = :i"),
                    {"i": first.organization_id},
                )
            ).scalar_one()
        assert count == 1

    async def test_same_key_different_payload_conflicts_deterministically(
        self, auth_client: Any, make_auth_user: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        key = f"replay-{uuid.uuid4().hex[:16]}"
        request = _onboarding(idempotency_key=key)
        await resources.provisioner.provision(
            request, caller_user_id=user.id, caller_has_platform_grant=True
        )
        altered = _onboarding(idempotency_key=key, display_name="Different Name")
        with pytest.raises(IdempotencyKeyConflictError):
            await resources.provisioner.provision(
                altered, caller_user_id=user.id, caller_has_platform_grant=True
            )

    async def test_duplicate_slug_conflicts_deterministically(
        self, auth_client: Any, make_auth_user: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        slug = f"same-slug-{uuid.uuid4().hex[:8]}"
        await resources.provisioner.provision(
            _onboarding(organization_key=slug),
            caller_user_id=user.id,
            caller_has_platform_grant=True,
        )
        with pytest.raises(OrganizationConflictError):
            await resources.provisioner.provision(
                _onboarding(organization_key=slug),
                caller_user_id=user.id,
                caller_has_platform_grant=True,
            )

    async def test_terminal_failure_replays_with_the_recorded_error(
        self, auth_client: Any, make_auth_user: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        # Occupy a slug, then claim a NEW key against the same slug: the Phase-2
        # conflict is a deterministic post-claim business failure.
        slug = f"terminal-{uuid.uuid4().hex[:8]}"
        await resources.provisioner.provision(
            _onboarding(organization_key=slug),
            caller_user_id=user.id,
            caller_has_platform_grant=True,
        )
        request = _onboarding(organization_key=slug)
        with pytest.raises(OrganizationConflictError) as first_exc:
            await resources.provisioner.provision(
                request, caller_user_id=user.id, caller_has_platform_grant=True
            )
        with pytest.raises(ProvisioningFailedError) as replay_exc:
            await resources.provisioner.provision(
                request, caller_user_id=user.id, caller_has_platform_grant=True
            )
        assert replay_exc.value.extensions["original_error_code"] == first_exc.value.code
        # The FAILED request is terminal: recorded with a sanitized code, no org id.
        async with resources.database.transaction() as session:
            ledger = (
                await session.execute(
                    text(
                        "SELECT status, organization_id, error_code FROM provisioning_requests "
                        "WHERE idempotency_key_hash = :kh"
                    ),
                    {"kh": request.idempotency_key_hash()},
                )
            ).one()
        assert ledger[0] == "FAILED"
        assert ledger[1] is None
        assert ledger[2] == "NXS_ORG_CONFLICT"

    async def test_pre_claim_owner_failure_is_deterministic_and_leaves_no_ledger_row(
        self, auth_client: Any, make_auth_user: Any
    ) -> None:
        from nexus_ai.core.errors import ProvisioningOwnerUnavailableError

        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        request = _onboarding(owner_user_id=uuid.uuid7())
        for _ in range(2):
            with pytest.raises(ProvisioningOwnerUnavailableError):
                await resources.provisioner.provision(
                    request, caller_user_id=user.id, caller_has_platform_grant=True
                )
        async with resources.database.transaction() as session:
            count = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM provisioning_requests "
                        "WHERE idempotency_key_hash = :kh"
                    ),
                    {"kh": request.idempotency_key_hash()},
                )
            ).scalar_one()
        assert count == 0  # validation precedes the claim: nothing to clean up

    async def test_pending_without_org_is_in_progress(
        self, auth_client: Any, make_auth_user: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        key = f"pending-{uuid.uuid4().hex[:16]}"
        request = _onboarding(idempotency_key=key)
        # Simulate a crash after the Phase-1 claim: a PENDING row with no Organization.
        async with resources.database.transaction() as session:
            await session.execute(
                text(
                    "INSERT INTO provisioning_requests "
                    "(id, idempotency_key_hash, request_fingerprint, organization_key, "
                    "status, created_by_user_id, owner_user_id, request_payload) "
                    "VALUES (:id, :kh, :fp, :ok, 'PENDING', :u, :u, CAST(:payload AS JSONB))"
                ),
                {
                    "id": uuid.uuid7(),
                    "kh": request.idempotency_key_hash(),
                    "fp": request.fingerprint(),
                    "ok": request.organization_key,
                    "u": user.id,
                    "payload": request.canonical_payload().decode(),  # JSON text cast to JSONB
                },
            )
        with pytest.raises(ProvisioningInProgressError):
            await resources.provisioner.provision(
                request, caller_user_id=user.id, caller_has_platform_grant=True
            )

    async def test_crash_between_phase2_and_phase3_resumes(
        self, auth_client: Any, make_auth_user: Any, tenant_database: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        request = _onboarding()
        result = await resources.provisioner.provision(
            request, caller_user_id=user.id, caller_has_platform_grant=True
        )
        # Simulate the crash: demote the COMPLETED row back to PENDING while the
        # Organization (and all its baseline rows) stay committed.
        async with resources.database.transaction() as session:
            await session.execute(
                text(
                    "UPDATE provisioning_requests SET status = 'PENDING' "
                    "WHERE idempotency_key_hash = :kh"
                ),
                {"kh": request.idempotency_key_hash()},
            )
        resumed = await resources.provisioner.provision(
            request, caller_user_id=user.id, caller_has_platform_grant=True
        )
        assert resumed.organization_id == result.organization_id
        async with resources.database.transaction() as session:
            status = (
                await session.execute(
                    text(
                        "SELECT status FROM provisioning_requests WHERE idempotency_key_hash = :kh"
                    ),
                    {"kh": request.idempotency_key_hash()},
                )
            ).scalar_one()
        assert status == "COMPLETED"


class TestAuthorizationAndEndpoint:
    async def test_api_requires_platform_capability(
        self, auth_client: Any, make_auth_org: Any, make_auth_user: Any
    ) -> None:
        home = await make_auth_org()
        email, _, _ = await make_auth_user(organization=home)
        session = await _login(auth_client, email, home.id)
        response = await auth_client.post(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {session['access_token']}"},
            json=_onboarding().model_dump(mode="json"),
        )
        assert response.status_code == 403
        assert response.json()["code"] == "NXS_CORE_PERMISSION_DENIED"
        assert response.json()["permission"] == "organization:create"

    async def test_api_provisions_end_to_end(
        self, auth_client: Any, make_auth_org: Any, make_auth_user: Any
    ) -> None:
        resources = _resources(auth_client)
        home = await make_auth_org()
        email, _, user = await make_auth_user(organization=home)
        await resources.provisioner.grant_create_capability(user_id=user.id)
        session = await _login(auth_client, email, home.id)
        payload = _onboarding().model_dump(mode="json")
        response = await auth_client.post(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {session['access_token']}"},
            json=payload,
        )
        assert response.status_code == 201
        body = response.json()
        assert body["organization_key"] == payload["organization_key"]
        assert body["provisioning_status"] == "COMPLETED"
        # Idempotent replay through the API returns the same result.
        replay = await auth_client.post(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {session['access_token']}"},
            json=payload,
        )
        assert replay.status_code == 201
        assert replay.json()["organization_id"] == body["organization_id"]

    async def test_member_permission_filtering_on_dashboard(
        self, auth_client: Any, make_auth_user: Any
    ) -> None:
        from nexus_ai.domain.auth.rbac import RoleKey

        resources = _resources(auth_client)
        _owner_email, _, owner = await make_auth_user()
        _member_email, _, member = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=owner.id)
        result = await resources.provisioner.provision(
            _onboarding(), caller_user_id=owner.id, caller_has_platform_grant=True
        )
        await resources.memberships.create(
            actor=None,
            organization_id=result.organization_id,
            user_id=member.id,
            role=RoleKey.ORG_MEMBER,
        )
        member_session = await _login(auth_client, _member_email, result.organization_id)
        dashboard = await auth_client.get(
            "/api/v1/organizations/current/dashboard-schema",
            headers={"Authorization": f"Bearer {member_session['access_token']}"},
        )
        assert dashboard.status_code == 200
        # org_member holds all three baseline read permissions, so the view is complete.
        widget_keys = {w["widget_key"] for s in dashboard.json()["sections"] for w in s["widgets"]}
        assert widget_keys == {
            "organization.profile",
            "organization.provisioning_status",
            "organization.settings_summary",
        }


async def _login(client: httpx.AsyncClient, email: str, org_id: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"email": email, "password": PASSWORD}
    if org_id is not None:
        payload["organization_id"] = str(org_id)
    response = await client.post("/api/v1/auth/login", json=payload)
    assert response.status_code == 200, response.text
    return response.json()
