"""Provisioning resilience matrix (NXS-ORG-001, NXS-DASH-001).

DB rollback behavior, crash/replay recovery, malformed stored dashboard
configurations, unsupported schema versions and repeated retries — every failure
mode leaves deterministic state and never a half-visible Organization.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.core.errors import (
    ProvisioningInProgressError,
    UnsupportedDashboardVersionError,
)
from nexus_ai.domain.provisioning.entities import OnboardingRequest

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


def _onboarding(**overrides: object) -> OnboardingRequest:
    payload: dict[str, object] = {
        "idempotency_key": f"res-{uuid.uuid4().hex[:16]}",
        "organization_key": f"res-org-{uuid.uuid4().hex[:8]}",
        "display_name": "Resilience Org",
        "legal_name": "Resilience Org S.A.",
        "country_code": "cr",
        "timezone": "America/Costa_Rica",
        "locale": "en-US",
    }
    payload.update(overrides)
    return OnboardingRequest(**payload)


def _resources(client: Any) -> Any:
    return client.nexus_app.state.lifespan.resources  # type: ignore[attr-defined]


class TestResilience:
    async def test_infrastructure_failure_rolls_back_everything_and_retry_is_clean(
        self, auth_client: Any, make_auth_user: Any
    ) -> None:
        from nexus_ai.domain.provisioning.service import OrganizationProvisioner

        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        # A provisioner that treats any PENDING claim as an abandoned crash (stale
        # threshold 0) — the deterministic resume model for a crashed Phase 1 claim.
        provisioner = OrganizationProvisioner(
            resources.settings,
            resources.database,
            resources.event_platform.publisher,
            pending_stale_seconds=0,
        )
        request = _onboarding()

        # Break the outbox insert mid-provisioning: a publish-platform failure must
        # roll back the WHOLE Phase-2 transaction (audit requirement).
        original_enqueue = resources.event_platform.publisher.enqueue
        calls = {"n": 0}

        async def _failing_enqueue(session: Any, envelope: Any) -> bool:
            calls["n"] += 1
            raise RuntimeError("simulated outbox insert failure")

        resources.event_platform.publisher.enqueue = _failing_enqueue  # type: ignore[method-assign]
        try:
            with pytest.raises(RuntimeError, match="simulated outbox insert failure"):
                await provisioner.provision(
                    request, caller_user_id=user.id, caller_has_platform_grant=True
                )
        finally:
            resources.event_platform.publisher.enqueue = original_enqueue  # type: ignore[method-assign]

        # Nothing operational was left behind: the request row is PENDING (Phase 1
        # committed), no Organization exists.
        async with resources.database.transaction() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT status, organization_id FROM provisioning_requests "
                        "WHERE idempotency_key_hash = :h"
                    ),
                    {"h": request.idempotency_key_hash()},
                )
            ).one()
        assert row[0] == "PENDING"
        assert row[1] is None

        # The SAME KEY resumes the abandoned claim and completes cleanly.
        result = await provisioner.provision(
            request, caller_user_id=user.id, caller_has_platform_grant=True
        )
        assert result.organization_status == "ACTIVE"

    async def test_malformed_stored_dashboard_configuration_fails_closed(
        self, auth_client: Any, make_auth_user: Any, tenant_database: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        result = await resources.provisioner.provision(
            _onboarding(), caller_user_id=user.id, caller_has_platform_grant=True
        )
        # Corrupt the stored configuration: an unknown widget key smuggled in.
        async with resources.database.tenant_transaction(result.organization_id) as tenant:
            await tenant.session.execute(
                text(
                    "UPDATE dashboard_configurations SET configuration = "
                    "jsonb_set(configuration, '{sections,0,widgets,0,widget_key}', "
                    "'\"tool_engine.execute\"')"
                )
            )
        from nexus_ai.domain.dashboard.service import DashboardSchemaService

        with pytest.raises(Exception) as excinfo:
            async with resources.database.tenant_transaction(result.organization_id) as tenant:
                await DashboardSchemaService().for_organization(
                    tenant, viewer_permissions={"dashboard:read"}
                )
        assert excinfo.value.__class__.__name__ in {
            "UnknownWidgetError",
            "DashboardSchemaError",
        }

    async def test_unsupported_schema_version_fails_closed(
        self, auth_client: Any, make_auth_user: Any, tenant_database: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        result = await resources.provisioner.provision(
            _onboarding(), caller_user_id=user.id, caller_has_platform_grant=True
        )
        async with resources.database.tenant_transaction(result.organization_id) as tenant:
            await tenant.session.execute(
                text("UPDATE dashboard_configurations SET schema_version = 99")
            )
        from nexus_ai.domain.dashboard.service import DashboardSchemaService

        with pytest.raises(UnsupportedDashboardVersionError):
            async with resources.database.tenant_transaction(result.organization_id) as tenant:
                await DashboardSchemaService().for_organization(
                    tenant, viewer_permissions={"dashboard:read"}
                )

    async def test_ready_organization_cannot_be_destructively_reprovisioned(
        self, auth_client: Any, make_auth_user: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        request = _onboarding()
        first = await resources.provisioner.provision(
            request, caller_user_id=user.id, caller_has_platform_grant=True
        )
        # Replaying with a NEW key but the same slug must conflict — never re-create
        # or reset the existing Organization.
        from nexus_ai.core.errors import OrganizationConflictError

        with pytest.raises(OrganizationConflictError):
            await resources.provisioner.provision(
                _onboarding(organization_key=request.organization_key),
                caller_user_id=user.id,
                caller_has_platform_grant=True,
            )
        # The original Organization is untouched.
        async with resources.database.tenant_transaction(first.organization_id) as tenant:
            status = (
                await tenant.session.execute(
                    text("SELECT status FROM organizations WHERE id = :i"),
                    {"i": first.organization_id},
                )
            ).scalar_one()
        assert status == "ACTIVE"

    async def test_repeated_retries_are_stable(self, auth_client: Any, make_auth_user: Any) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        request = _onboarding()
        first = await resources.provisioner.provision(
            request, caller_user_id=user.id, caller_has_platform_grant=True
        )
        for _ in range(5):
            replay = await resources.provisioner.provision(
                request, caller_user_id=user.id, caller_has_platform_grant=True
            )
            assert replay.organization_id == first.organization_id

    async def test_pending_claim_without_work_keeps_in_progress(
        self, auth_client: Any, make_auth_user: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        request = _onboarding()
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
                    "payload": request.canonical_payload().decode(),  # JSON text; CAST(:payload AS JSONB)
                },
            )
        with pytest.raises(ProvisioningInProgressError):
            await resources.provisioner.provision(
                request, caller_user_id=user.id, caller_has_platform_grant=True
            )
