"""C01-C20: real PostgreSQL control-row ordering, not process-lock authority."""

import asyncio
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from nexus_ai.compliance.contracts import (
    ApprovalInput,
    ClaimInput,
    ComplianceConflict,
    ExecutionFence,
    HoldInput,
    PolicyInput,
    RequestKind,
    RetentionInput,
    SubjectRequestInput,
    VerificationInput,
)
from nexus_ai.domain.compliance.models import ComplianceExecution, ComplianceHold, CompliancePolicy
from nexus_ai.domain.customers.models import CustomerRecord
from tests.compliance_helpers import approved, claimed, planned, policy, request

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def race(*operations):
    barrier = asyncio.Barrier(len(operations))

    async def contender(operation):
        await barrier.wait()
        return await operation()

    return await asyncio.gather(
        *(contender(operation) for operation in operations), return_exceptions=True
    )


def successes(results):
    for result in results:
        if isinstance(result, BaseException):
            assert isinstance(result, ComplianceConflict), repr(result)
    return [result for result in results if not isinstance(result, BaseException)]


async def hold(env):
    return await env.service.create_hold(
        env.principal, HoldInput(subject_id=env.customer.id, reason_code="LEGAL")
    )


async def verify(env, row):
    await env.service.verify_request(
        env.principal,
        uuid.UUID(row["id"]),
        VerificationInput(method_code="OP", evidence_reference="REF"),
    )


async def wait_database_lease(env, plan):
    async with env.database.tenant_transaction(env.organization.id) as tenant:
        await tenant.session.execute(
            text(
                "SELECT pg_sleep(GREATEST(0, EXTRACT(EPOCH FROM "
                "(lease_expires_at - clock_timestamp()))) + 0.01) "
                "FROM compliance_executions WHERE plan_id = :plan"
            ),
            {"plan": plan},
        )


async def test_c01_concurrent_policy_activation(compliance_env):
    env = compliance_env
    payload = PolicyInput.model_validate(
        {"rules": [{"resource_class": "CUSTOMER_PROFILE", "days": 1, "action": "RETAIN"}]}
    )
    row = await env.service.create_policy(env.principal, payload)
    results = await race(
        *(
            lambda: env.service.activate_policy(env.principal, uuid.UUID(row["id"]))
            for _ in range(2)
        )
    )
    assert len(successes(results)) == 1
    async with env.database.tenant_transaction(env.organization.id) as tenant:
        assert (
            await tenant.session.scalar(
                select(func.count())
                .select_from(CompliancePolicy)
                .where(CompliancePolicy.state == "ACTIVE")
            )
            == 1
        )


async def test_c02_retirement_vs_planning(compliance_env):
    env = compliance_env
    active = await policy(env)
    row = await request(env)
    await verify(env, row)
    results = await race(
        lambda: env.service.retire_policy(env.principal, uuid.UUID(active["id"])),
        lambda: env.service.plan_request(env.principal, uuid.UUID(row["id"])),
    )
    successes(results)
    assert results[0] is None
    if isinstance(results[1], dict):
        with pytest.raises(ComplianceConflict):
            await env.service.approve(
                env.principal, uuid.UUID(results[1]["id"]), ApprovalInput(reason_code="STALE")
            )


async def test_c03_policy_revision_vs_approval(compliance_env):
    env = compliance_env
    _, plan = await planned(env)
    results = await race(
        lambda: policy(env),
        lambda: env.service.approve(env.principal, plan, ApprovalInput(reason_code="OK")),
    )
    successes(results)
    with pytest.raises(ComplianceConflict):
        await env.service.claim(env.principal, plan, ClaimInput(owner_id=uuid.uuid4()))


async def test_c04_hold_vs_erasure_planning(compliance_env):
    env = compliance_env
    await policy(env)
    row = await request(env)
    await verify(env, row)
    results = await race(
        lambda: hold(env), lambda: env.service.plan_request(env.principal, uuid.UUID(row["id"]))
    )
    successes(results)
    assert isinstance(results[0], dict)
    if isinstance(results[1], dict):
        with pytest.raises(ComplianceConflict):
            await env.service.approve(
                env.principal, uuid.UUID(results[1]["id"]), ApprovalInput(reason_code="NO")
            )


async def test_c05_hold_vs_final_destructive_effect(compliance_env):
    env = compliance_env
    _, plan, fence = await claimed(env)
    results = await race(lambda: hold(env), lambda: env.service.execute(env.principal, plan, fence))
    successes(results)
    async with env.database.tenant_transaction(env.organization.id) as tenant:
        target = await tenant.session.get(CustomerRecord, env.customer.id)
        assert target.version == (2 if isinstance(results[1], dict) else 1)
        assert await tenant.session.scalar(select(func.count()).select_from(ComplianceHold)) == 1


async def test_c06_release_does_not_revive_stale_authorization(compliance_env):
    env = compliance_env
    _, plan, fence = await claimed(env)
    held = await hold(env)
    results = await race(
        lambda: env.service.release_hold(env.principal, uuid.UUID(held["id"])),
        lambda: env.service.execute(env.principal, plan, fence),
    )
    successes(results)
    assert isinstance(results[1], ComplianceConflict)


async def test_c07_duplicate_subject_request(compliance_env):
    env = compliance_env
    payload = SubjectRequestInput(
        subject_id=env.customer.id, kind=RequestKind.ACCESS, idempotency_key="duplicate"
    )
    results = await race(
        *(lambda: env.service.create_request(env.principal, payload) for _ in range(4))
    )
    assert len(successes(results)) == 4
    assert len({row["id"] for row in results}) == 1


async def test_c08_approve_vs_deny(compliance_env):
    env = compliance_env
    identity, plan = await planned(env)
    results = await race(
        lambda: env.service.approve(env.principal, plan, ApprovalInput(reason_code="OK")),
        lambda: env.service.end_request(env.principal, identity, "DENIED"),
    )
    successes(results)
    assert (await env.service.get_request(env.principal, identity))["state"] == "DENIED"
    with pytest.raises(ComplianceConflict):
        await env.service.claim(env.principal, plan, ClaimInput(owner_id=uuid.uuid4()))


async def test_c09_cancel_vs_approve(compliance_env):
    env = compliance_env
    identity, plan = await planned(env)
    results = await race(
        lambda: env.service.approve(env.principal, plan, ApprovalInput(reason_code="OK")),
        lambda: env.service.end_request(env.principal, identity, "CANCELLED"),
    )
    successes(results)
    assert (await env.service.get_request(env.principal, identity))["state"] == "CANCELLED"


async def test_c10_cancel_vs_claim(compliance_env):
    env = compliance_env
    identity, plan = await approved(env)
    results = await race(
        lambda: env.service.claim(env.principal, plan, ClaimInput(owner_id=uuid.uuid4())),
        lambda: env.service.end_request(env.principal, identity, "CANCELLED"),
    )
    assert len(successes(results)) == 1
    assert (await env.service.get_request(env.principal, identity))["state"] in {
        "CANCELLED",
        "EXECUTING",
    }


async def test_c11_two_claimants(compliance_env):
    env = compliance_env
    _, plan = await approved(env)
    results = await race(
        *(
            lambda: env.service.claim(env.principal, plan, ClaimInput(owner_id=uuid.uuid4()))
            for _ in range(2)
        )
    )
    assert len(successes(results)) == 1


async def test_c12_expired_lease_takeover(compliance_env):
    env = compliance_env
    _, plan = await approved(env)
    owner = uuid.uuid4()
    await env.service.claim(env.principal, plan, ClaimInput(owner_id=owner, lease_seconds=1))
    await wait_database_lease(env, plan)
    results = await race(
        *(
            lambda: env.service.claim(env.principal, plan, ClaimInput(owner_id=uuid.uuid4()))
            for _ in range(2)
        )
    )
    winner = successes(results)
    assert len(winner) == 1 and winner[0]["generation"] == 2


async def test_c13_stale_worker_cannot_finalize_takeover(compliance_env):
    env = compliance_env
    _, plan = await approved(env)
    owner = uuid.uuid4()
    await env.service.claim(env.principal, plan, ClaimInput(owner_id=owner, lease_seconds=1))
    await wait_database_lease(env, plan)
    new_owner = uuid.uuid4()
    await env.service.claim(env.principal, plan, ClaimInput(owner_id=new_owner))
    results = await race(
        lambda: env.service.execute(
            env.principal, plan, ExecutionFence(owner_id=owner, generation=1)
        ),
        lambda: env.service.execute(
            env.principal, plan, ExecutionFence(owner_id=new_owner, generation=2)
        ),
    )
    assert len(successes(results)) == 1
    assert isinstance(results[0], ComplianceConflict)


async def test_c14_duplicate_execution_retry(compliance_env):
    env = compliance_env
    _, plan, fence = await claimed(env)
    results = await race(
        *(lambda: env.service.execute(env.principal, plan, fence) for _ in range(2))
    )
    assert len(successes(results)) == 2 and results[0] == results[1]
    async with env.database.tenant_transaction(env.organization.id) as tenant:
        assert (await tenant.session.get(CustomerRecord, env.customer.id)).version == 2


async def test_c15_target_drift_after_approval(compliance_env):
    env = compliance_env
    _, plan, fence = await claimed(env)
    async with env.database.tenant_transaction(env.organization.id) as tenant:
        await tenant.session.execute(
            text("UPDATE customers SET version = version + 1 WHERE id = :id"),
            {"id": env.customer.id},
        )
    with pytest.raises(ComplianceConflict):
        await env.service.execute(env.principal, plan, fence)


async def test_c16_same_subject_two_organizations(compliance_env, make_auth_org):
    env = compliance_env
    foreign = await make_auth_org()
    barrier = asyncio.Barrier(2)

    async def read(organization):
        async with env.database.tenant_transaction(organization) as tenant:
            await barrier.wait()
            return await tenant.session.get(CustomerRecord, env.customer.id)

    own, other = await asyncio.gather(read(env.organization.id), read(foreign.id))
    assert own is not None and other is None


async def test_c17_cross_tenant_forged_resource(compliance_env, make_auth_org):
    env = compliance_env
    foreign = await make_auth_org()
    _, plan = await planned(env)
    async with env.database.tenant_transaction(foreign.id) as tenant:
        assert (
            await tenant.session.scalar(
                text("SELECT id FROM compliance_plans WHERE id = :id"), {"id": plan}
            )
            is None
        )
        result = await tenant.session.execute(
            text("UPDATE compliance_requests SET state = 'CANCELLED'")
        )
        assert result.rowcount == 0


async def test_c18_new_unsupported_coverage_cannot_disappear(compliance_env):
    env = compliance_env
    identity, plan, fence = await claimed(env)

    async def discover():
        async with env.database.tenant_transaction(env.organization.id) as tenant:
            await tenant.session.execute(text("SELECT id FROM compliance_controls FOR UPDATE"))
            await tenant.session.execute(
                text(
                    "UPDATE compliance_requests SET incomplete_resources = "
                    "incomplete_resources || '[\"NEW_DOMAIN\"]'::jsonb WHERE id = :id"
                ),
                {"id": identity},
            )

    results = await race(discover, lambda: env.service.execute(env.principal, plan, fence))
    assert len(successes(results)) == 2
    row = await env.service.get_request(env.principal, identity)
    assert row["state"] == "PARTIAL" and "NEW_DOMAIN" in row["incomplete_resources"]


async def test_c19_retention_evaluation_vs_hold(compliance_env):
    env = compliance_env
    await policy(env)
    async with env.database.tenant_transaction(env.organization.id) as tenant:
        await tenant.session.execute(
            text("UPDATE customers SET created_at = now() - interval '31 days' WHERE id = :id"),
            {"id": env.customer.id},
        )
    results = await race(
        lambda: hold(env),
        lambda: env.service.evaluate_retention(
            env.principal, RetentionInput(subject_id=env.customer.id, idempotency_key="retention")
        ),
    )
    successes(results)
    if isinstance(results[1], dict):
        with pytest.raises(ComplianceConflict):
            await env.service.approve(
                env.principal, uuid.UUID(results[1]["id"]), ApprovalInput(reason_code="NO")
            )


async def test_c20_outbox_failure_rolls_back_effect(compliance_env, monkeypatch):
    env = compliance_env
    _, plan, fence = await claimed(env)

    async def fail(session, envelope):
        await session.execute(
            text("INSERT INTO event_outbox (id) VALUES (:id)"), {"id": uuid.uuid4()}
        )

    monkeypatch.setattr(env.service._publisher, "enqueue", fail)
    with pytest.raises(DBAPIError):
        await env.service.execute(env.principal, plan, fence)
    async with env.database.tenant_transaction(env.organization.id) as tenant:
        assert (await tenant.session.get(CustomerRecord, env.customer.id)).version == 1
        assert (
            await tenant.session.scalar(
                select(ComplianceExecution).where(ComplianceExecution.plan_id == plan)
            )
        ).state == "CLAIMED"


async def test_lease_expiry_during_target_lock_wait_blocks_effect(compliance_env, monkeypatch):
    env = compliance_env
    _, plan = await approved(env)
    owner = uuid.uuid4()
    await env.service.claim(env.principal, plan, ClaimInput(owner_id=owner, lease_seconds=1))
    reached = asyncio.Event()
    original = env.service._profile.locked_target

    async def observed_lock(tenant, identity):
        reached.set()
        return await original(tenant, identity)

    monkeypatch.setattr(env.service._profile, "locked_target", observed_lock)
    async with env.database.tenant_transaction(env.organization.id) as tenant:
        await tenant.session.execute(
            select(CustomerRecord).where(CustomerRecord.id == env.customer.id).with_for_update()
        )
        execution = asyncio.create_task(
            env.service.execute(env.principal, plan, ExecutionFence(owner_id=owner, generation=1))
        )
        await asyncio.wait_for(reached.wait(), timeout=5)
        await wait_database_lease(env, plan)
    with pytest.raises(ComplianceConflict, match="lease expired before effect"):
        await execution
    async with env.database.tenant_transaction(env.organization.id) as tenant:
        assert (await tenant.session.get(CustomerRecord, env.customer.id)).version == 1
