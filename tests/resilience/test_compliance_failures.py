"""Fail-closed local effects, crash recovery and unsupported external dispatch."""

import datetime as dt
import uuid

import pytest
from sqlalchemy import text

from nexus_ai.compliance.contracts import (
    ApprovalInput,
    ClaimInput,
    ComplianceConflict,
    ExecutionFence,
    HoldInput,
    ResourceClass,
    RetentionInput,
)
from nexus_ai.core.errors import NotFoundError, PermissionDeniedError
from tests.compliance_helpers import approved, claimed, planned, policy

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_expired_approval_cannot_claim(compliance_env):
    env = compliance_env
    _, plan = await approved(env, lifetime=1)
    async with env.database.tenant_transaction(env.organization.id) as tenant:
        await tenant.session.execute(
            text(
                "SELECT pg_sleep(GREATEST(0, EXTRACT(EPOCH FROM "
                "(expires_at - clock_timestamp()))) + 0.01) FROM compliance_approvals "
                "WHERE plan_id = :plan"
            ),
            {"plan": plan},
        )
    with pytest.raises(ComplianceConflict, match="expired approval"):
        await env.service.claim(env.principal, plan, ClaimInput(owner_id=uuid.uuid4()))


async def test_crash_before_claim_has_no_effect_and_requires_owner(compliance_env):
    env = compliance_env
    _, plan = await approved(env)
    with pytest.raises(ComplianceConflict, match="owner/generation"):
        await env.service.execute(
            env.principal, plan, ExecutionFence(owner_id=uuid.uuid4(), generation=1)
        )
    async with env.database.tenant_transaction(env.organization.id) as tenant:
        assert (
            await tenant.session.scalar(
                text("SELECT version FROM customers WHERE id = :id"), {"id": env.customer.id}
            )
            == 1
        )


async def test_unregistered_external_adapter_never_dispatches(compliance_env):
    env = compliance_env
    await policy(env)
    for resource in ResourceClass:
        if resource == ResourceClass.CUSTOMER_PROFILE:
            continue
        with pytest.raises(ComplianceConflict, match="unsupported retention adapter"):
            await env.service.evaluate_retention(
                env.principal,
                RetentionInput(
                    subject_id=env.customer.id,
                    resource_class=resource,
                    idempotency_key="unsupported",
                ),
            )


async def test_adapter_proven_pre_effect_failure_rolls_back(compliance_env, monkeypatch):
    env = compliance_env
    identity, plan, fence = await claimed(env)

    def reject(*args):
        raise ComplianceConflict("adapter rejected before local mutation")

    monkeypatch.setattr(env.service._profile, "anonymize", reject)
    with pytest.raises(ComplianceConflict, match="before local mutation"):
        await env.service.execute(env.principal, plan, fence)
    assert (await env.service.get_request(env.principal, identity))["state"] == "EXECUTING"
    async with env.database.tenant_transaction(env.organization.id) as tenant:
        assert (
            await tenant.session.scalar(
                text("SELECT version FROM customers WHERE id = :id"), {"id": env.customer.id}
            )
            == 1
        )


async def test_missing_rows_and_repeated_terminal_operations(compliance_env):
    env = compliance_env
    missing = uuid.uuid4()
    for operation in (
        lambda: env.service.activate_policy(env.principal, missing),
        lambda: env.service.retire_policy(env.principal, missing),
        lambda: env.service.release_hold(env.principal, missing),
        lambda: env.service.approve(env.principal, missing, ApprovalInput(reason_code="NO")),
    ):
        with pytest.raises(NotFoundError):
            await operation()
    active = await policy(env)
    with pytest.raises(ComplianceConflict):
        await env.service.activate_policy(env.principal, uuid.UUID(active["id"]))
    await env.service.retire_policy(env.principal, uuid.UUID(active["id"]))
    with pytest.raises(ComplianceConflict):
        await env.service.retire_policy(env.principal, uuid.UUID(active["id"]))
    with pytest.raises(ComplianceConflict):
        await env.service.end_request(env.principal, missing, "COMPLETED")


async def test_expired_principal_and_duplicate_approval(compliance_env):
    env = compliance_env
    with pytest.raises(PermissionDeniedError):
        await env.service.list_requests(
            env.principal.model_copy(
                update={"expires_at": dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)}
            )
        )
    _, plan = await approved(env)
    with pytest.raises(ComplianceConflict, match="immutable approval"):
        await env.service.approve(env.principal, plan, ApprovalInput(reason_code="AGAIN"))


async def test_current_hold_without_epoch_shortcut_blocks(compliance_env):
    env = compliance_env
    _, plan = await planned(env)
    await env.service.create_hold(
        env.principal, HoldInput(subject_id=env.customer.id, reason_code="LEGAL")
    )
    with pytest.raises(ComplianceConflict):
        await env.service.approve(env.principal, plan, ApprovalInput(reason_code="NO"))


async def test_expiry_is_rechecked_after_claim(compliance_env):
    env = compliance_env
    _, plan = await approved(env, lifetime=1)
    owner = uuid.uuid4()
    await env.service.claim(env.principal, plan, ClaimInput(owner_id=owner))
    async with env.database.tenant_transaction(env.organization.id) as tenant:
        await tenant.session.execute(
            text(
                "SELECT pg_sleep(GREATEST(0, EXTRACT(EPOCH FROM "
                "(expires_at - clock_timestamp()))) + 0.01) FROM compliance_approvals "
                "WHERE plan_id = :plan"
            ),
            {"plan": plan},
        )
    with pytest.raises(ComplianceConflict, match="expired approval"):
        await env.service.execute(env.principal, plan, ExecutionFence(owner_id=owner, generation=1))


async def test_database_unavailable_cannot_cross_effect_boundary(compliance_env, pool1_database):
    from nexus_ai.compliance.service import ComplianceService
    from nexus_ai.core.errors import DependencyUnavailableError

    env = compliance_env
    identity, plan, fence = await claimed(env)
    isolated_service = ComplianceService(
        pool1_database, env.resources.event_platform.publisher, service_name="isolated-test"
    )
    await pool1_database.disconnect()
    with pytest.raises(DependencyUnavailableError):
        await isolated_service.execute(env.principal, plan, fence)
    assert (await env.service.get_request(env.principal, identity))["state"] == "EXECUTING"
    async with env.database.tenant_transaction(env.organization.id) as tenant:
        assert (
            await tenant.session.scalar(
                text("SELECT version FROM customers WHERE id = :id"), {"id": env.customer.id}
            )
            == 1
        )
