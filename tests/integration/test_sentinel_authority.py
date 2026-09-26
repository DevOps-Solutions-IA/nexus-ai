"""Live authentication and subsystem boundaries, without synthetic tenant authority."""

from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy import text

from nexus_ai.cells.contracts import PlacementMutation
from nexus_ai.core.health import HealthStatus
from nexus_ai.domain.auth.state import PrincipalStateValidator
from nexus_ai.sentinel.adapters import (
    CellHealthProbe,
    PlacementHealthProbe,
    SipTelephonyHealthProbe,
)
from nexus_ai.sentinel.control import SentinelOperatorAuthority, SentinelPermission
from nexus_ai.sentinel.errors import SentinelDenied
from nexus_ai.sip_edge.contracts import RegisterTarget, TargetMutation
from nexus_ai.sip_edge.targets import TargetNetworkPolicy, TargetRegistry
from tests.conftest import SUPERUSER_DSN
from tests.integration.test_cell_placement import placement_setup as placement_setup
from tests.integration.test_sip_target_constraints import target_control as target_control

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_live_tenant_token_cannot_authorize_sentinel(
    auth_client, make_auth_org, make_auth_user, login_helper
):
    organization = await make_auth_org()
    email, password, user = await make_auth_user(organization=organization)
    token = (await login_helper(email, password))["access_token"]
    resources = auth_client.nexus_app.state.lifespan.resources
    authority = SentinelOperatorAuthority(
        resources.token_service, PrincipalStateValidator(resources.database), resources.database
    )
    for permission in SentinelPermission:
        with pytest.raises(SentinelDenied, match="platform_grant"):
            await authority.require(token, permission)
    async with resources.database.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO platform_grants (user_id, capability) VALUES (:actor, 'sentinel:read')"
            ),
            {"actor": user.id},
        )
    assert await authority.require(token, SentinelPermission.READ) == user.id
    with pytest.raises(SentinelDenied):
        await authority.require(token, SentinelPermission.CONTROL)
    administrator = await asyncpg.connect(SUPERUSER_DSN)
    try:
        await administrator.execute("DELETE FROM platform_grants WHERE user_id=$1", user.id)
    finally:
        await administrator.close()
    with pytest.raises(SentinelDenied):
        await authority.require(token, SentinelPermission.READ)


async def test_native_cell_probes_preserve_p18_authority(placement_setup):
    organization, actor, service, cell = placement_setup
    assert (
        await CellHealthProbe(service, actor=actor.user_id, cell_id=cell)()
    ).status == HealthStatus.UP
    await service.mutate(
        organization.id,
        actor.user_id,
        PlacementMutation(
            cell_id=cell, operation="ASSIGN", idempotency_key=uuid4().hex, reason_code="TEST"
        ),
        uuid4(),
    )
    probe = PlacementHealthProbe(service, actor=actor.user_id, organization_id=organization.id)
    assert (await probe()).status == HealthStatus.UP
    await service.mutate(
        organization.id,
        actor.user_id,
        PlacementMutation(
            cell_id=cell,
            operation="SUSPEND",
            expected_generation=1,
            idempotency_key=uuid4().hex,
            reason_code="TEST",
        ),
        uuid4(),
    )
    assert (await probe()).status == HealthStatus.DOWN


async def test_native_sip_probe_uses_p19_target_authority(target_control, tenant_database):
    cell, actor = target_control
    registry = TargetRegistry(
        tenant_database, TargetNetworkPolicy(("10.0.0.0/8",), frozenset({5060}))
    )
    result = await registry.register(
        actor,
        RegisterTarget(
            cell_id=cell,
            host="10.1.2.3",
            port=5060,
            transport="UDP",
            expected_revision=0,
            idempotency_key=uuid4().hex,
            reason_code="TEST",
        ),
        uuid4(),
    )
    probe = SipTelephonyHealthProbe(registry, actor=actor, cell_id=cell, target_id=result.target_id)
    assert (await probe()).status == HealthStatus.DOWN
    await registry.transition(
        actor,
        TargetMutation(
            cell_id=cell,
            target_id=result.target_id,
            expected_revision=result.control_revision,
            state="ACTIVE",
            idempotency_key=uuid4().hex,
            reason_code="TEST",
        ),
        uuid4(),
    )
    assert (await probe()).status == HealthStatus.UP
