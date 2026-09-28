"""Actual PostgreSQL policy, request, approval and atomic profile execution."""

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from nexus_ai.compliance.contracts import (
    ApprovalInput,
    ClaimInput,
    ComplianceConflict,
    HoldInput,
    RequestKind,
    RetentionInput,
    SubjectRequestInput,
    VerificationInput,
)
from nexus_ai.domain.compliance.models import CompliancePolicy
from tests.compliance_helpers import approved, claimed, planned, policy, request

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.mark.parametrize("kind", list(RequestKind))
async def test_request_executes_profile_only_and_reports_partial(compliance_env, kind):
    env = compliance_env
    identity, plan, fence = await claimed(env, kind=kind)
    result = await env.service.execute(env.principal, plan, fence)
    assert result["state"] == "COMPLETED"
    assert await env.service.execute(env.principal, plan, fence) == result
    assert await env.service.claim(env.principal, plan, ClaimInput(owner_id=uuid.uuid4())) == result
    request_row = await env.service.get_request(env.principal, identity)
    assert request_row["state"] == "PARTIAL"
    assert "CUSTOMER_IDENTITIES" in request_row["incomplete_resources"]
    assert "CREDENTIALS" in request_row["incomplete_resources"]
    assert len(await env.service.list_requests(env.principal, limit=1)) == 1
    assert await env.service.list_requests(env.principal, after=identity) == []


async def test_legal_hold_blocks_planning_and_stale_approval_after_release(compliance_env):
    env = compliance_env
    identity, plan = await approved(env)
    hold = await env.service.create_hold(
        env.principal, HoldInput(subject_id=env.customer.id, reason_code="LEGAL")
    )
    with pytest.raises(ComplianceConflict):
        await env.service.claim(env.principal, plan, ClaimInput(owner_id=uuid.uuid4()))
    await env.service.release_hold(env.principal, uuid.UUID(hold["id"]))
    with pytest.raises(ComplianceConflict):
        await env.service.claim(env.principal, plan, ClaimInput(owner_id=uuid.uuid4()))
    with pytest.raises(ComplianceConflict):
        await env.service.release_hold(env.principal, uuid.UUID(hold["id"]))
    assert (await env.service.get_request(env.principal, identity))["state"] == "APPROVED"


async def test_duplicate_request_and_payload_conflict(compliance_env):
    env = compliance_env
    payload = SubjectRequestInput(
        subject_id=env.customer.id, kind=RequestKind.ACCESS, idempotency_key="same"
    )
    first = await env.service.create_request(env.principal, payload)
    assert await env.service.create_request(env.principal, payload) == first
    with pytest.raises(ComplianceConflict):
        await env.service.create_request(
            env.principal, payload.model_copy(update={"kind": RequestKind.ERASURE})
        )


async def test_policy_revision_invalidates_existing_plan(compliance_env):
    env = compliance_env
    _, plan = await planned(env)
    await policy(env)
    with pytest.raises(ComplianceConflict):
        await env.service.approve(env.principal, plan, ApprovalInput(reason_code="STALE"))
    async with env.database.tenant_transaction(env.organization.id) as tenant:
        rows = (await tenant.session.scalars(select(CompliancePolicy))).all()
        assert [row.state for row in sorted(rows, key=lambda row: row.revision)] == [
            "RETIRED",
            "ACTIVE",
        ]


async def test_policy_content_is_database_immutable(compliance_env):
    env = compliance_env
    row = await policy(env)
    with pytest.raises(DBAPIError):
        async with env.database.tenant_transaction(env.organization.id) as tenant:
            await tenant.session.execute(
                text("UPDATE compliance_policies SET rules = '[]' WHERE id = :id"),
                {"id": uuid.UUID(row["id"])},
            )


@pytest.mark.parametrize("target", ["DENIED", "CANCELLED", "EXPIRED"])
async def test_terminal_request_cannot_plan_or_verify(compliance_env, target):
    env = compliance_env
    row = await request(env)
    identity = uuid.UUID(row["id"])
    assert (await env.service.end_request(env.principal, identity, target))["state"] == target
    with pytest.raises(ComplianceConflict):
        await env.service.verify_request(
            env.principal, identity, VerificationInput(method_code="OP", evidence_reference="REF")
        )
    with pytest.raises(ComplianceConflict):
        await env.service.plan_request(env.principal, identity)


async def test_retention_evaluation_never_mutates_new_target(compliance_env):
    env = compliance_env
    await policy(env)
    result = await env.service.evaluate_retention(
        env.principal, RetentionInput(subject_id=env.customer.id, idempotency_key="retention")
    )
    assert result["action"] == "RETAIN"


async def test_retention_due_requires_approval_and_replays_stable_plan(compliance_env):
    env = compliance_env
    await policy(env)
    async with env.database.tenant_transaction(env.organization.id) as tenant:
        await tenant.session.execute(
            text("UPDATE customers SET created_at = now() - interval '31 days' WHERE id = :id"),
            {"id": env.customer.id},
        )
    payload = RetentionInput(subject_id=env.customer.id, idempotency_key="retention")
    first = await env.service.evaluate_retention(env.principal, payload)
    second = await env.service.evaluate_retention(env.principal, payload)
    assert first["id"] == second["id"]
    with pytest.raises(ComplianceConflict):
        await env.service.claim(
            env.principal, uuid.UUID(first["id"]), ClaimInput(owner_id=uuid.uuid4())
        )


async def test_unverified_and_missing_policy_fail_closed(compliance_env):
    env = compliance_env
    row = await request(env)
    identity = uuid.UUID(row["id"])
    with pytest.raises(ComplianceConflict):
        await env.service.plan_request(env.principal, identity)
    await env.service.verify_request(
        env.principal, identity, VerificationInput(method_code="OP", evidence_reference="REF")
    )
    with pytest.raises(ComplianceConflict):
        await env.service.plan_request(env.principal, identity)
