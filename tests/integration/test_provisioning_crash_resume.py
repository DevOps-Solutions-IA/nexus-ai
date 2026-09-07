"""Crash-resume attribution regression matrix (independent-audit Finding A).

A provisioning request may resume/finalize committed work ONLY when that Organization
is exactly attributable to the SAME request — the Phase-2 link is FK-enforced, and a
slug must never adopt another request's Organization. Every audit scenario is pinned
here, with database constraints as the final authority.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.core.errors import (
    OrganizationConflictError,
    ProvisioningFailedError,
    ProvisioningInProgressError,
)
from nexus_ai.domain.provisioning.entities import OnboardingRequest, ProvisioningResult
from nexus_ai.domain.provisioning.service import OrganizationProvisioner

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


def _onboarding(**overrides: object) -> OnboardingRequest:
    payload: dict[str, object] = {
        "idempotency_key": f"cr-{uuid.uuid4().hex[:16]}",
        "organization_key": f"cr-org-{uuid.uuid4().hex[:8]}",
        "display_name": "Crash Org",
        "legal_name": "Crash Org S.A.",
        "country_code": "cr",
        "timezone": "America/Costa_Rica",
        "locale": "en-US",
    }
    payload.update(overrides)
    return OnboardingRequest(**payload)


def _resources(client: Any) -> Any:
    return client.nexus_app.state.lifespan.resources  # type: ignore[attr-defined]


async def _row(resources: Any, key_hash: str) -> tuple[str, Any, Any]:
    async with resources.database.transaction() as session:
        return (
            await session.execute(
                text(
                    "SELECT status, organization_id, id FROM provisioning_requests "
                    "WHERE idempotency_key_hash = :kh"
                ),
                {"kh": key_hash},
            )
        ).one()


def _zero_stale_provisioner(resources: Any) -> OrganizationProvisioner:
    return OrganizationProvisioner(
        resources.settings,
        resources.database,
        resources.event_platform.publisher,
        pending_stale_seconds=0,
    )


class TestCrashResumeAttribution:
    async def test_phase2_commit_phase3_crash_same_key_recovers_exactly(
        self, auth_client: Any, make_auth_user: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        request = _onboarding()
        result = await resources.provisioner.provision(
            request, caller_user_id=user.id, caller_has_platform_grant=True
        )
        # Simulate the crash window: Phase 2 committed (organization_id linked),
        # Phase 3 never ran (status demoted back to PENDING).
        async with resources.database.transaction() as session:
            await session.execute(
                text(
                    "UPDATE provisioning_requests SET status = 'PENDING' "
                    "WHERE idempotency_key_hash = :kh"
                ),
                {"kh": request.idempotency_key_hash()},
            )
        # Same idempotency key + same payload: exact original Organization, and THAT
        # request is finalized — never a different request id.
        recovered = await resources.provisioner.provision(
            request, caller_user_id=user.id, caller_has_platform_grant=True
        )
        assert recovered.organization_id == result.organization_id
        status, org_id, _request_id = await _row(resources, request.idempotency_key_hash())
        assert status == "COMPLETED"
        assert org_id == result.organization_id
        async with resources.database.tenant_transaction(result.organization_id) as tenant:
            count = (
                await tenant.session.execute(
                    text("SELECT count(*) FROM organizations WHERE id = :i"),
                    {"i": result.organization_id},
                )
            ).scalar_one()
        assert count == 1

    async def test_different_key_same_slug_never_adopts_the_first_organization(
        self, auth_client: Any, make_auth_user: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        slug = f"adopt-{uuid.uuid4().hex[:8]}"
        first = await resources.provisioner.provision(
            _onboarding(organization_key=slug),
            caller_user_id=user.id,
            caller_has_platform_grant=True,
        )
        # A DIFFERENT key against the same slug: deterministic conflict, never the
        # first request's Organization.
        second_request = _onboarding(organization_key=slug)
        with pytest.raises(OrganizationConflictError):
            await resources.provisioner.provision(
                second_request, caller_user_id=user.id, caller_has_platform_grant=True
            )
        # The winner's request stays COMPLETED; the loser recorded its own FAILED row
        # (post-claim path) and never touched the winner's row.
        first_status, first_org, _first_id = await _row(
            resources, await first_requests_key_hash(resources, slug)
        )
        assert first_status == "COMPLETED"
        assert first_org == first.organization_id
        loser_status, loser_org, _loser_id = await _row(
            resources, second_request.idempotency_key_hash()
        )
        assert loser_status == "FAILED"
        assert loser_org is None

    async def test_different_key_same_slug_different_payload_conflicts(
        self, auth_client: Any, make_auth_user: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        slug = f"payload-{uuid.uuid4().hex[:8]}"
        first = await resources.provisioner.provision(
            _onboarding(organization_key=slug, display_name="Variant One"),
            caller_user_id=user.id,
            caller_has_platform_grant=True,
        )
        other = _onboarding(organization_key=slug, display_name="Variant Two")
        with pytest.raises(OrganizationConflictError):
            await resources.provisioner.provision(
                other, caller_user_id=user.id, caller_has_platform_grant=True
            )
        # Replay of the losing key yields the recorded terminal failure, never the
        # winner's Organization.
        with pytest.raises(ProvisioningFailedError):
            await resources.provisioner.provision(
                other, caller_user_id=user.id, caller_has_platform_grant=True
            )
        # Exactly one Organization exists for the slug.
        async with resources.database.tenant_transaction(first.organization_id) as tenant:
            count = (
                await tenant.session.execute(
                    text("SELECT count(*) FROM organizations WHERE organization_key = :k"),
                    {"k": slug},
                )
            ).scalar_one()
        assert count == 1

    async def test_stale_pending_never_adopts_an_organization_created_by_another_request(
        self, auth_client: Any, make_auth_user: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        provisioner = _zero_stale_provisioner(resources)
        slug = f"stale-{uuid.uuid4().hex[:8]}"
        # Request B claims first (crashes with no org, link NULL), then request A
        # completes the same slug.
        request_b = _onboarding(organization_key=slug)
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
                    "kh": request_b.idempotency_key_hash(),
                    "fp": request_b.fingerprint(),
                    "ok": slug,
                    "u": user.id,
                    "payload": request_b.canonical_payload().decode(),
                },
            )
        first = await provisioner.provision(
            _onboarding(organization_key=slug),
            caller_user_id=user.id,
            caller_has_platform_grant=True,
        )
        # B's stale resume re-runs the baseline: the slug conflict is deterministic,
        # and B MUST NOT return A's Organization.
        with pytest.raises(OrganizationConflictError):
            await provisioner.provision(
                request_b, caller_user_id=user.id, caller_has_platform_grant=True
            )
        b_status, b_org, _ = await _row(resources, request_b.idempotency_key_hash())
        assert b_status == "FAILED"
        assert b_org is None  # never adopted
        assert first.organization_id is not None

    async def test_wrong_request_never_marked_completed(
        self, auth_client: Any, make_auth_user: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        provisioner = _zero_stale_provisioner(resources)
        slug = f"wrong-{uuid.uuid4().hex[:8]}"
        request_b = _onboarding(organization_key=slug)
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
                    "kh": request_b.idempotency_key_hash(),
                    "fp": request_b.fingerprint(),
                    "ok": slug,
                    "u": user.id,
                    "payload": request_b.canonical_payload().decode(),
                },
            )
        # A different key completes the slug first.
        await provisioner.provision(
            _onboarding(organization_key=slug),
            caller_user_id=user.id,
            caller_has_platform_grant=True,
        )
        # B resumes and fails on the slug; verify NO other request's row was
        # completed by B's resume path.
        with pytest.raises(OrganizationConflictError):
            await provisioner.provision(
                request_b, caller_user_id=user.id, caller_has_platform_grant=True
            )
        async with resources.database.transaction() as session:
            completed_count = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM provisioning_requests "
                        "WHERE organization_key = :k AND status = 'COMPLETED'"
                    ),
                    {"k": slug},
                )
            ).scalar_one()
        assert completed_count == 1  # exactly the winner's request

    async def test_original_request_does_not_remain_pending_after_recovery(
        self, auth_client: Any, make_auth_user: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        request = _onboarding()
        await resources.provisioner.provision(
            request, caller_user_id=user.id, caller_has_platform_grant=True
        )
        async with resources.database.transaction() as session:
            await session.execute(
                text(
                    "UPDATE provisioning_requests SET status = 'PENDING' "
                    "WHERE idempotency_key_hash = :kh"
                ),
                {"kh": request.idempotency_key_hash()},
            )
        await resources.provisioner.provision(
            request, caller_user_id=user.id, caller_has_platform_grant=True
        )
        status, _, _ = await _row(resources, request.idempotency_key_hash())
        assert status == "COMPLETED"  # never left PENDING

    async def test_concurrent_different_key_same_slug_exactly_one_winner(
        self, auth_client: Any, make_auth_user: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        slug = f"race-{uuid.uuid4().hex[:8]}"

        async def _provision() -> Any:
            try:
                return await resources.provisioner.provision(
                    _onboarding(organization_key=slug),
                    caller_user_id=user.id,
                    caller_has_platform_grant=True,
                )
            except Exception as exc:
                return exc

        results = await asyncio.gather(_provision(), _provision(), _provision())
        winners = [r for r in results if isinstance(r, ProvisioningResult)]
        conflicts = [r for r in results if isinstance(r, OrganizationConflictError)]
        assert len(winners) == 1
        assert len(conflicts) == 2
        async with resources.database.tenant_transaction(winners[0].organization_id) as tenant:
            count = (
                await tenant.session.execute(
                    text("SELECT count(*) FROM organizations WHERE organization_key = :k"),
                    {"k": slug},
                )
            ).scalar_one()
        assert count == 1

    async def test_fresh_pending_from_another_worker_still_in_progress(
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
                    "payload": request.canonical_payload().decode(),
                },
            )
        with pytest.raises(ProvisioningInProgressError):
            await resources.provisioner.provision(
                request, caller_user_id=user.id, caller_has_platform_grant=True
            )


async def first_requests_key_hash(resources: Any, slug: str) -> str:
    async with resources.database.transaction() as session:
        return (
            await session.execute(
                text(
                    "SELECT idempotency_key_hash FROM provisioning_requests "
                    "WHERE organization_key = :k AND status = 'COMPLETED'"
                ),
                {"k": slug},
            )
        ).scalar_one()
