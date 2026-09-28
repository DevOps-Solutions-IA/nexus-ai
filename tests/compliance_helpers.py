"""Real PostgreSQL fixture operations shared by compliance certification layers."""

import uuid

from nexus_ai.compliance.contracts import (
    ApprovalInput,
    ClaimInput,
    ExecutionFence,
    PolicyInput,
    RequestKind,
    SubjectRequestInput,
    VerificationInput,
)


async def policy(env):
    row = await env.service.create_policy(
        env.principal,
        PolicyInput.model_validate(
            {
                "rules": [
                    {"resource_class": "CUSTOMER_PROFILE", "days": 30, "action": "ANONYMIZE"}
                ],
            }
        ),
    )
    await env.service.activate_policy(env.principal, uuid.UUID(row["id"]))
    return row


async def request(env, *, kind=RequestKind.ERASURE):
    return await env.service.create_request(
        env.principal,
        SubjectRequestInput(
            subject_id=env.customer.id,
            kind=kind,
            idempotency_key=uuid.uuid4().hex,
        ),
    )


async def planned(env, *, kind=RequestKind.ERASURE):
    await policy(env)
    row = await request(env, kind=kind)
    await env.service.verify_request(
        env.principal,
        uuid.UUID(row["id"]),
        VerificationInput(
            method_code="OPERATOR_VERIFIED",
            evidence_reference="opaque-reference",
        ),
    )
    plan = await env.service.plan_request(env.principal, uuid.UUID(row["id"]))
    return uuid.UUID(row["id"]), uuid.UUID(plan["id"])


async def approved(env, *, kind=RequestKind.ERASURE, lifetime=300):
    identity, plan = await planned(env, kind=kind)
    await env.service.approve(
        env.principal,
        plan,
        ApprovalInput(
            reason_code="AUTHORIZED",
            lifetime_seconds=lifetime,
        ),
    )
    return identity, plan


async def claimed(env, *, kind=RequestKind.ERASURE):
    identity, plan = await approved(env, kind=kind)
    owner = uuid.uuid4()
    claim = await env.service.claim(env.principal, plan, ClaimInput(owner_id=owner))
    return identity, plan, ExecutionFence(owner_id=owner, generation=claim["generation"])
