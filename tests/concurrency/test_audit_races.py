"""C01-C20 exercise PostgreSQL audit authority and explicit transaction barriers."""

import asyncio
import datetime as dt
import uuid
from itertools import pairwise

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text

from nexus_ai.audit.consumer import AuditReceiptStore, consume_intent, register_audit_consumer
from nexus_ai.audit.contracts import (
    AuditActor,
    AuditConflict,
    AuditIntent,
    AuditMetadata,
    AuditProvenanceError,
)
from nexus_ai.audit.producer import emit_audit
from nexus_ai.audit.repository import AuditRepository
from nexus_ai.domain.audit.models import AuditHead, AuditRecord
from nexus_ai.domain.events.models import ConsumerReceiptRecord, EventOutboxRecord
from nexus_ai.events.consumer import EventContext
from nexus_ai.events.registry import EVENT_REGISTRY

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


def intent_for(organization_id, **changes):
    values = {
        "organization_id": organization_id,
        "source_id": uuid.uuid4(),
        "producer": "customer",
        "action": "customer.created",
        "target_type": "customer",
        "target_id": uuid.uuid4(),
        "actor": AuditActor(kind="HUMAN", user_id=uuid.uuid4()),
        "occurred_at": dt.datetime.now(dt.UTC),
    }
    return AuditIntent.model_validate(values | changes)


async def append(database, intent):
    async with database.tenant_transaction(intent.organization_id) as tenant:
        return await AuditRepository(tenant).append(intent)


async def race(*operations):
    barrier = asyncio.Barrier(len(operations))

    async def contender(operation):
        await barrier.wait()
        return await operation()

    return await asyncio.wait_for(
        asyncio.gather(*(contender(operation) for operation in operations)), timeout=10
    )


async def source(database, organization_id, *, actor=None):
    async with database.tenant_transaction(organization_id) as tenant:
        return await emit_audit(
            tenant,
            producer="customer",
            action="customer.created",
            target_type="customer",
            target_id=uuid.uuid4(),
            actor=actor or AuditActor(kind="HUMAN", user_id=uuid.uuid4()),
        )


async def ingest(database, envelope, *, crash=False):
    async with database.tenant_transaction(envelope.organization_id) as tenant:
        receipts = AuditReceiptStore()
        claim = await receipts.claim(
            tenant.session, consumer_name="audit-ledger-v1", envelope=envelope, delivery_count=1
        )
        if not claim.already_finished:
            await consume_intent(
                EventContext(envelope, EVENT_REGISTRY.decode(envelope), tenant.session, 1)
            )
            if crash:
                raise RuntimeError("injected before transaction commit")
            await receipts.mark_processed(
                tenant.session, consumer_name="audit-ledger-v1", event_id=envelope.event_id
            )
        return claim


async def verify(database, organization_id, count):
    async with database.tenant_transaction(organization_id) as tenant:
        result = await AuditRepository(tenant).verify()
        assert result.valid and result.complete and result.checked == count
        return result


async def test_c01_same_organization_concurrent_append(tenant_database, make_organization):
    organization = await make_organization()
    first, second = intent_for(organization.id), intent_for(organization.id)
    rows = await race(
        lambda: append(tenant_database, first), lambda: append(tenant_database, second)
    )
    assert {row.sequence for row in rows} == {1, 2}
    await verify(tenant_database, organization.id, 2)


async def test_c02_other_organization_does_not_wait_on_head(tenant_database, make_organization):
    first, second = await make_organization(), await make_organization()
    await append(tenant_database, intent_for(first.id))
    async with tenant_database.tenant_transaction(first.id) as tenant:
        await tenant.session.scalar(select(AuditHead).with_for_update())
        row = await asyncio.wait_for(append(tenant_database, intent_for(second.id)), timeout=3)
        assert row.sequence == 1
    await verify(tenant_database, first.id, 1)
    await verify(tenant_database, second.id, 1)


async def test_c03_duplicate_delivery_converges(tenant_database, make_organization):
    organization = await make_organization()
    envelope = await source(tenant_database, organization.id)
    assert not (await ingest(tenant_database, envelope)).already_finished
    assert (await ingest(tenant_database, envelope)).already_finished
    await verify(tenant_database, organization.id, 1)


async def test_c04_concurrent_duplicate_receipts(tenant_database, make_organization):
    organization = await make_organization()
    envelope = await source(tenant_database, organization.id)
    results = await race(
        lambda: ingest(tenant_database, envelope), lambda: ingest(tenant_database, envelope)
    )
    assert sorted(result.already_finished for result in results) == [False, True]
    await verify(tenant_database, organization.id, 1)


async def test_c05_same_identity_changed_content_rejected(tenant_database, make_organization):
    organization = await make_organization()
    intent = intent_for(organization.id)
    original = await append(tenant_database, intent)
    with pytest.raises(AuditConflict):
        await append(tenant_database, intent.model_copy(update={"outcome": "FAILED"}))
    assert (await append(tenant_database, intent)).id == original.id
    await verify(tenant_database, organization.id, 1)


async def test_c06_crash_after_insert_rolls_back_receipt_and_fact(
    tenant_database, make_organization
):
    organization = await make_organization()
    envelope = await source(tenant_database, organization.id)
    with pytest.raises(RuntimeError, match="injected"):
        await ingest(tenant_database, envelope, crash=True)
    await verify(tenant_database, organization.id, 0)
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        assert (
            await tenant.session.scalar(
                select(ConsumerReceiptRecord).where(
                    ConsumerReceiptRecord.event_id == envelope.event_id
                )
            )
            is None
        )
    assert not (await ingest(tenant_database, envelope)).already_finished
    await verify(tenant_database, organization.id, 1)


async def test_c07_commit_then_lost_ack_replay(tenant_database, make_organization):
    organization = await make_organization()
    envelope = await source(tenant_database, organization.id)
    await ingest(tenant_database, envelope)
    before = await verify(tenant_database, organization.id, 1)
    await ingest(tenant_database, envelope)
    after = await verify(tenant_database, organization.id, 1)
    assert before.head_digest == after.head_digest


async def test_c08_chain_assignment_many_contenders(tenant_database, make_organization):
    organization = await make_organization()
    intents = [intent_for(organization.id) for _ in range(6)]
    rows = await race(
        *(lambda intent=intent: append(tenant_database, intent) for intent in intents)
    )
    assert sorted(row.sequence for row in rows) == list(range(1, 7))
    ordered = sorted(rows, key=lambda row: row.sequence)
    assert all(current.predecessor == previous.digest for previous, current in pairwise(ordered))
    await verify(tenant_database, organization.id, 6)


async def test_c09_stale_predecessor_cannot_be_supplied(tenant_database, make_organization):
    organization = await make_organization()
    first = await append(tenant_database, intent_for(organization.id))
    second = await append(tenant_database, intent_for(organization.id))
    supplied = intent_for(organization.id).model_dump() | {"predecessor": first.digest}
    with pytest.raises(ValidationError):
        AuditIntent.model_validate(supplied)
    third = await append(tenant_database, intent_for(organization.id))
    assert third.predecessor == second.digest
    await verify(tenant_database, organization.id, 3)


async def test_c10_verifier_sees_consistent_committed_snapshot(tenant_database, make_organization):
    organization = await make_organization()
    await append(tenant_database, intent_for(organization.id))
    async with tenant_database.tenant_transaction(organization.id) as writer:
        await AuditRepository(writer).append(intent_for(organization.id))
        await verify(tenant_database, organization.id, 1)
    await verify(tenant_database, organization.id, 2)


async def test_c11_foreign_tenant_cannot_change_integrity_head(tenant_database, make_organization):
    first, second = await make_organization(), await make_organization()
    await append(tenant_database, intent_for(first.id))
    async with tenant_database.tenant_transaction(second.id) as tenant:
        changed = await tenant.session.execute(
            text("UPDATE audit_heads SET sequence=900 WHERE organization_id=:organization"),
            {"organization": first.id},
        )
        assert changed.rowcount == 0
        with pytest.raises(AuditProvenanceError):
            await AuditRepository(tenant).append(intent_for(first.id))
    await verify(tenant_database, first.id, 1)


async def test_c12_foreign_record_id_not_visible(tenant_database, make_organization):
    first, second = await make_organization(), await make_organization()
    row = await append(tenant_database, intent_for(first.id))
    async with tenant_database.tenant_transaction(second.id) as tenant:
        assert await AuditRepository(tenant).get(row.id) is None
        assert (
            await tenant.session.scalar(select(AuditRecord).where(AuditRecord.id == row.id)) is None
        )


async def test_c13_snapshot_cursor_excludes_new_appends(tenant_database, make_organization):
    organization = await make_organization()
    for _ in range(3):
        await append(tenant_database, intent_for(organization.id))
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        first = await AuditRepository(tenant).page(limit=2)
    await append(tenant_database, intent_for(organization.id))
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        second = await AuditRepository(tenant).page(
            after=first["next_after"], high_water=first["high_water"], limit=2
        )
    assert [row["sequence"] for row in first["records"] + second["records"]] == [1, 2, 3]
    assert second["next_after"] is None


async def test_c14_correction_waits_for_original_commit_without_rewriting(
    tenant_database, make_organization
):
    organization = await make_organization()
    entered = asyncio.Event()
    async with tenant_database.tenant_transaction(organization.id) as writer:
        original = await AuditRepository(writer).append(intent_for(organization.id))
        correction = intent_for(
            organization.id,
            metadata=AuditMetadata(original_record_id=original.id),
            outcome="FAILED",
        )

        async def correct():
            async with tenant_database.tenant_transaction(organization.id) as tenant:
                entered.set()
                return await AuditRepository(tenant).append(correction)

        pending = asyncio.create_task(correct())
        await asyncio.wait_for(entered.wait(), timeout=3)
        assert not pending.done()
    recorded = await asyncio.wait_for(pending, timeout=3)
    assert recorded.sequence == 2 and recorded.predecessor == original.digest
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        unchanged = await AuditRepository(tenant).get(original.id)
        assert unchanged.digest == original.digest and unchanged.fact == original.fact
    await verify(tenant_database, organization.id, 2)


async def test_c15_source_mutation_and_intent_rollback(tenant_database, make_organization):
    organization = await make_organization()
    with pytest.raises(RuntimeError, match="source rollback"):
        async with tenant_database.tenant_transaction(organization.id) as tenant:
            await tenant.session.execute(
                text("UPDATE organizations SET display_name='rolled-back' WHERE id=:identity"),
                {"identity": organization.id},
            )
            envelope = await emit_audit(
                tenant,
                producer="organization",
                action="organization.profile_updated",
                target_type="organization",
                target_id=organization.id,
                actor=AuditActor(kind="SERVICE", service="organization-service"),
            )
            raise RuntimeError("source rollback")
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        assert (
            await tenant.session.scalar(
                select(EventOutboxRecord).where(EventOutboxRecord.id == envelope.event_id)
            )
            is None
        )
        assert (
            await tenant.session.scalar(
                text("SELECT display_name FROM organizations WHERE id=:identity"),
                {"identity": organization.id},
            )
            == organization.display_name
        )
    with pytest.raises(AuditProvenanceError):
        await ingest(tenant_database, envelope)


async def test_c16_uncommitted_intent_not_consumable(tenant_database, make_organization):
    organization = await make_organization()
    async with tenant_database.tenant_transaction(organization.id) as writer:
        envelope = await emit_audit(
            writer,
            producer="customer",
            action="customer.created",
            target_type="customer",
            target_id=uuid.uuid4(),
            actor=AuditActor(kind="SERVICE", service="customer-service"),
        )
        with pytest.raises(AuditProvenanceError):
            await ingest(tenant_database, envelope)
    await ingest(tenant_database, envelope)
    await verify(tenant_database, organization.id, 1)


async def test_c17_revocation_does_not_rewrite_occurred_fact(
    tenant_database, make_auth_org, make_auth_user
):
    organization = await make_auth_org()
    _, _, user = await make_auth_user(organization=organization)
    envelope = await source(
        tenant_database, organization.id, actor=AuditActor(kind="HUMAN", user_id=user.id)
    )
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        await tenant.session.execute(
            text(
                "UPDATE memberships SET status='REVOKED' "
                "WHERE organization_id=:organization AND user_id=:user"
            ),
            {"organization": organization.id, "user": user.id},
        )
    await ingest(tenant_database, envelope)
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        row = await tenant.session.scalar(select(AuditRecord))
        assert row.fact["intent"]["actor"]["user_id"] == str(user.id)
    await verify(tenant_database, organization.id, 1)


async def test_c18_same_actor_and_source_id_across_organizations(
    tenant_database, make_organization
):
    first, second = await make_organization(), await make_organization()
    shared = {"source_id": uuid.uuid4(), "actor": AuditActor(kind="HUMAN", user_id=uuid.uuid4())}
    rows = await race(
        lambda: append(tenant_database, intent_for(first.id, **shared)),
        lambda: append(tenant_database, intent_for(second.id, **shared)),
    )
    assert rows[0].id != rows[1].id and rows[0].digest != rows[1].digest
    await verify(tenant_database, first.id, 1)
    await verify(tenant_database, second.id, 1)


async def test_c19_poison_terminal_preserves_no_success(
    event_platform, tenant_database, make_organization, integration_env, nats_messaging
):
    organization = await make_organization()
    envelope = await source(tenant_database, organization.id)
    consumer = register_audit_consumer(event_platform, tenant_database, integration_env())
    await consumer.ensure_subscription()
    forged = envelope.model_copy(update={"payload": {**envelope.payload, "producer": "unknown"}})
    subject = event_platform.publisher.subject_for(forged)
    for attempt in range(2):
        await nats_messaging.jetstream().publish(
            subject, forged.to_json(), headers={"Nats-Msg-Id": f"poison-{uuid.uuid4()}"}
        )
        assert await consumer.run_pending(max_messages=1, timeout=2) == 1
        assert consumer.stats.processed == 0 and consumer.stats.retried == 0
        assert consumer.stats.dead_lettered == attempt + 1
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        row = await tenant.session.scalar(
            select(ConsumerReceiptRecord).where(ConsumerReceiptRecord.event_id == envelope.event_id)
        )
        assert row.status == "DEAD"
    await verify(tenant_database, organization.id, 0)


async def test_c20_ledger_append_does_not_reemit_audit(tenant_database, make_organization):
    organization = await make_organization()
    envelope = await source(tenant_database, organization.id)
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        before = await tenant.session.scalar(select(func.count()).select_from(EventOutboxRecord))
    await ingest(tenant_database, envelope)
    await ingest(tenant_database, envelope)
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        after = await tenant.session.scalar(select(func.count()).select_from(EventOutboxRecord))
        assert before == after
    await verify(tenant_database, organization.id, 1)
