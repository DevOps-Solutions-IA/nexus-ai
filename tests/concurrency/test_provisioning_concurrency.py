"""Provisioning concurrency matrix (NXS-ORG-001).

Database constraints and transaction semantics are the final authority — no Python
locks. Same-key races yield one Organization, same-slug races yield exactly one
winner, owner assignments never duplicate, and the dashboard baseline resolves to
one canonical revision.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.core.errors import IdempotencyKeyConflictError, OrganizationConflictError
from nexus_ai.domain.provisioning.entities import OnboardingRequest

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


def _onboarding(**overrides: object) -> OnboardingRequest:
    payload: dict[str, object] = {
        "idempotency_key": f"cc-{uuid.uuid4().hex[:16]}",
        "organization_key": f"cc-org-{uuid.uuid4().hex[:8]}",
        "display_name": "Concurrent Org",
        "legal_name": "Concurrent Org S.A.",
        "country_code": "cr",
        "timezone": "America/Costa_Rica",
        "locale": "en-US",
    }
    payload.update(overrides)
    return OnboardingRequest(**payload)


def _resources(client: Any) -> Any:
    return client.nexus_app.state.lifespan.resources  # type: ignore[attr-defined]


async def _org_count(resources: Any, organization_key: str) -> int:
    async with resources.database.transaction() as session:
        return (
            await session.execute(
                text("SELECT count(*) FROM provisioning_requests WHERE organization_key = :k"),
                {"k": organization_key},
            )
        ).scalar_one()


class TestConcurrentProvisioning:
    async def test_two_simultaneous_identical_requests_one_organization(
        self, auth_client: Any, make_auth_user: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        request = _onboarding()

        async def _provision() -> Any:
            try:
                return await resources.provisioner.provision(
                    request, caller_user_id=user.id, caller_has_platform_grant=True
                )
            except Exception as exc:
                return exc

        results = await asyncio.gather(_provision(), _provision())
        organization_ids = {r.organization_id for r in results if hasattr(r, "organization_id")}
        assert len(organization_ids) == 1
        async with resources.database.tenant_transaction(next(iter(organization_ids))) as tenant:
            count = (
                await tenant.session.execute(
                    text("SELECT count(*) FROM organizations WHERE id = :i"),
                    {"i": next(iter(organization_ids))},
                )
            ).scalar_one()
        assert count == 1

    async def test_ten_simultaneous_identical_requests_one_organization(
        self, auth_client: Any, make_auth_user: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        request = _onboarding()

        async def _provision() -> Any:
            try:
                return await resources.provisioner.provision(
                    request, caller_user_id=user.id, caller_has_platform_grant=True
                )
            except Exception as exc:
                return exc

        results = await asyncio.gather(*[_provision() for _ in range(10)])
        organization_ids = {r.organization_id for r in results if hasattr(r, "organization_id")}
        assert len(organization_ids) == 1

    async def test_same_key_different_payload_races_to_deterministic_conflict(
        self, auth_client: Any, make_auth_user: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        key = f"race-key-{uuid.uuid4().hex[:16]}"
        request_a = _onboarding(idempotency_key=key, display_name="Variant A")
        request_b = _onboarding(idempotency_key=key, display_name="Variant B")

        async def _provision(request: OnboardingRequest) -> Any:
            try:
                return await resources.provisioner.provision(
                    request, caller_user_id=user.id, caller_has_platform_grant=True
                )
            except Exception as exc:
                return exc

        results = await asyncio.gather(_provision(request_a), _provision(request_b))
        winners = [r for r in results if hasattr(r, "organization_id")]
        conflicts = [r for r in results if isinstance(r, IdempotencyKeyConflictError)]
        assert len(winners) == 1
        assert len(conflicts) == 1

    async def test_different_keys_same_slug_exactly_one_wins(
        self, auth_client: Any, make_auth_user: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        slug = f"slug-race-{uuid.uuid4().hex[:8]}"

        async def _provision() -> Any:
            try:
                return await resources.provisioner.provision(
                    _onboarding(organization_key=slug),
                    caller_user_id=user.id,
                    caller_has_platform_grant=True,
                )
            except Exception as exc:
                return exc

        results = await asyncio.gather(_provision(), _provision())
        winners = [r for r in results if hasattr(r, "organization_id")]
        conflicts = [r for r in results if isinstance(r, OrganizationConflictError)]
        assert len(winners) == 1
        assert len(conflicts) == 1

    async def test_owner_assignment_never_duplicates(
        self, auth_client: Any, make_auth_user: Any, tenant_database: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        request = _onboarding()

        async def _provision() -> Any:
            try:
                return await resources.provisioner.provision(
                    request, caller_user_id=user.id, caller_has_platform_grant=True
                )
            except Exception as exc:
                return exc

        results = await asyncio.gather(_provision(), _provision(), _provision())
        organization_id = next(r.organization_id for r in results if hasattr(r, "organization_id"))
        async with tenant_database.tenant_transaction(organization_id) as tenant:
            assignments = (
                await tenant.session.execute(
                    text(
                        "SELECT count(*) FROM role_assignments WHERE user_id = :u "
                        "AND organization_id = :o"
                    ),
                    {"u": user.id, "o": organization_id},
                )
            ).scalar_one()
            memberships = (
                await tenant.session.execute(
                    text(
                        "SELECT count(*) FROM memberships WHERE user_id = :u "
                        "AND organization_id = :o"
                    ),
                    {"u": user.id, "o": organization_id},
                )
            ).scalar_one()
        assert memberships == 1
        assert assignments == 1

    async def test_dashboard_baseline_resolves_to_one_canonical_revision(
        self, auth_client: Any, make_auth_user: Any, tenant_database: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        request = _onboarding()

        async def _provision() -> Any:
            try:
                return await resources.provisioner.provision(
                    request, caller_user_id=user.id, caller_has_platform_grant=True
                )
            except Exception as exc:
                return exc

        results = await asyncio.gather(_provision(), _provision())
        organization_id = next(r.organization_id for r in results if hasattr(r, "organization_id"))
        async with tenant_database.tenant_transaction(organization_id) as tenant:
            revisions = (
                await tenant.session.execute(
                    text(
                        "SELECT revision, count(*) FROM dashboard_configurations GROUP BY revision"
                    )
                )
            ).all()
        assert len(revisions) == 1
        assert revisions[0][0] == 1
        assert revisions[0][1] == 1
