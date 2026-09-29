import uuid

import pytest
from sqlalchemy import select

from nexus_ai.audit.contracts import AuditActor, AuditProvenanceError
from nexus_ai.audit.producer import emit_audit
from nexus_ai.domain.events.models import ConsumerReceiptRecord

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.mark.parametrize("safe_correlation", [True, False])
async def test_source_preserves_safe_request_correlation(
    tenant_database,
    make_organization,
    safe_correlation,
):
    from nexus_ai.core.context import request_context

    organization = await make_organization()
    request_id, correlation_id, source_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with request_context(
        request_id=str(request_id),
        correlation_id=str(correlation_id) if safe_correlation else "untrusted-secret-value",
    ):
        async with tenant_database.tenant_transaction(organization.id) as tenant:
            envelope = await emit_audit(
                tenant,
                producer="customer",
                action="customer.created",
                target_type="customer",
                target_id=uuid.uuid4(),
                source_id=source_id,
                actor=AuditActor(kind="HUMAN", user_id=uuid.uuid4()),
            )
    assert envelope.payload["request_id"] == str(request_id)
    assert envelope.payload["correlation_id"] == (str(correlation_id) if safe_correlation else None)
    assert envelope.payload["causation_id"] == str(source_id)
    assert "untrusted-secret-value" not in str(envelope.payload)


async def test_committed_source_and_finished_receipt_replay(tenant_database, make_organization):
    from nexus_ai.audit.consumer import AuditReceiptStore, consume_intent
    from nexus_ai.events.consumer import EventContext
    from nexus_ai.events.registry import EVENT_REGISTRY

    organization = await make_organization()
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        envelope = await emit_audit(
            tenant,
            producer="customer",
            action="customer.created",
            target_type="customer",
            target_id=uuid.uuid4(),
            actor=AuditActor(kind="HUMAN", user_id=uuid.uuid4()),
        )
    receipts = AuditReceiptStore()
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        claim = await receipts.claim(
            tenant.session, consumer_name="audit-ledger-v1", envelope=envelope, delivery_count=1
        )
        assert not claim.already_finished
        await consume_intent(
            EventContext(envelope, EVENT_REGISTRY.decode(envelope), tenant.session, 1)
        )
        await receipts.mark_processed(
            tenant.session, consumer_name="audit-ledger-v1", event_id=envelope.event_id
        )
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        assert (
            await receipts.claim(
                tenant.session, consumer_name="audit-ledger-v1", envelope=envelope, delivery_count=2
            )
        ).already_finished
    forged = envelope.model_copy(update={"payload": {**envelope.payload, "outcome": "FAILED"}})
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        with pytest.raises(AuditProvenanceError):
            await receipts.claim(
                tenant.session, consumer_name="audit-ledger-v1", envelope=forged, delivery_count=3
            )
        row = await tenant.session.scalar(
            select(ConsumerReceiptRecord).where(
                ConsumerReceiptRecord.event_id == envelope.event_id,
            )
        )
        assert row.status == "PROCESSED"


async def test_rolled_back_source_cannot_be_ingested(tenant_database, make_organization):
    from nexus_ai.audit.consumer import AuditReceiptStore

    organization = await make_organization()
    with pytest.raises(RuntimeError):
        async with tenant_database.tenant_transaction(organization.id) as tenant:
            envelope = await emit_audit(
                tenant,
                producer="customer",
                action="customer.created",
                target_type="customer",
                target_id=uuid.uuid4(),
                actor=AuditActor(kind="HUMAN", user_id=uuid.uuid4()),
            )
            raise RuntimeError("source rolled back")
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        with pytest.raises(AuditProvenanceError):
            await AuditReceiptStore().claim(
                tenant.session, consumer_name="audit-ledger-v1", envelope=envelope, delivery_count=1
            )
