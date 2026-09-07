"""Organization service: lifecycle, suspension and optimistic concurrency (sections 58, 59)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nexus_ai.core.errors import (
    OrganizationInactiveError,
    OrganizationInvalidStateError,
    OrganizationNotFoundError,
    OrganizationVersionConflictError,
)
from nexus_ai.core.tenancy import TenantContext, TenantContextSource
from nexus_ai.domain.organizations.entities import OrganizationDraft, OrganizationProfileUpdate
from nexus_ai.domain.organizations.status import OrganizationStatus

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


def _ctx(organization_id: Any) -> TenantContext:
    return TenantContext(organization_id, TenantContextSource.SYSTEM_BOOTSTRAP)


async def test_create_core_record_is_provisioning(organization_service: Any) -> None:
    draft = OrganizationDraft(
        organization_key="lifecycle-a",
        display_name="Lifecycle A",
        legal_name="Lifecycle A SA",
        country_code="cr",
        timezone="America/Costa_Rica",
    )
    org = await organization_service.create_core_record(draft)
    assert org.status is OrganizationStatus.PROVISIONING
    assert org.version == 1
    assert org.activated_at is None
    assert org.id.version == 7  # UUIDv7


async def test_suspension_blocks_then_reactivation_restores(
    organization_service: Any, make_organization
) -> None:
    org = await make_organization(activate=True)
    context = _ctx(org.id)
    assert (
        await organization_service.require_operational(context)
    ).status is OrganizationStatus.ACTIVE

    await organization_service.transition(org.id, OrganizationStatus.SUSPENDED)
    with pytest.raises(OrganizationInactiveError):
        await organization_service.require_operational(context)
    with pytest.raises(OrganizationInactiveError):
        await organization_service.update_profile(
            context, OrganizationProfileUpdate(display_name="X", expected_version=99)
        )

    await organization_service.transition(org.id, OrganizationStatus.ACTIVE)
    assert (
        await organization_service.require_operational(context)
    ).status is OrganizationStatus.ACTIVE

    await organization_service.transition(org.id, OrganizationStatus.ARCHIVED)
    with pytest.raises(OrganizationInactiveError):
        await organization_service.require_operational(context)
    with pytest.raises(OrganizationInvalidStateError):
        await organization_service.transition(org.id, OrganizationStatus.ACTIVE)


async def test_optimistic_concurrency_prevents_lost_update(
    organization_service: Any, make_organization
) -> None:
    org = await make_organization(activate=True)
    context = _ctx(org.id)
    current = await organization_service.get_current(context)
    stale_version = current.version

    winner, loser = await asyncio.gather(
        organization_service.update_profile(
            context,
            OrganizationProfileUpdate(display_name="Winner", expected_version=stale_version),
        ),
        organization_service.update_profile(
            context, OrganizationProfileUpdate(display_name="Loser", expected_version=stale_version)
        ),
        return_exceptions=True,
    )
    outcomes = [winner, loser]
    successes = [o for o in outcomes if not isinstance(o, Exception)]
    failures = [o for o in outcomes if isinstance(o, OrganizationVersionConflictError)]
    assert len(successes) == 1
    assert len(failures) == 1
    final = await organization_service.get_current(context)
    assert final.version == stale_version + 1
    assert final.display_name == successes[0].display_name


async def test_get_current_not_found_for_unknown_scope(organization_service: Any) -> None:
    import uuid

    with pytest.raises(OrganizationNotFoundError):
        await organization_service.get_current(_ctx(uuid.uuid7()))


async def test_timestamps_are_timezone_aware_and_consistent(
    organization_service: Any, make_organization
) -> None:
    org = await make_organization(activate=True)
    fresh = await organization_service.get_current(_ctx(org.id))
    assert fresh.created_at.tzinfo is not None
    assert fresh.activated_at is not None
    assert fresh.activated_at >= fresh.created_at
    await organization_service.transition(org.id, OrganizationStatus.SUSPENDED)
    suspended = await organization_service.get_current(_ctx(org.id))
    assert suspended.suspended_at is not None
    assert suspended.suspended_at >= suspended.activated_at
