"""Audit delivery uses real PostgreSQL commits and a deterministic ACK transport."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from nexus_ai.audit.consumer import AuditConsumer, AuditReceiptStore, consume_intent
from nexus_ai.audit.contracts import AuditActor, AuditIntent
from nexus_ai.audit.producer import emit_audit
from nexus_ai.domain.audit.models import AuditRecord
from nexus_ai.domain.events.models import ConsumerReceiptRecord, EventDeadLetterRecord
from nexus_ai.events.consumer import ConsumerSpec
from nexus_ai.events.errors import HandlerRetryableError
from nexus_ai.infrastructure.event_dead_letter import DeadLetterRepository

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def source(database, organization_id):
    async with database.tenant_transaction(organization_id) as tenant:
        return await emit_audit(
            tenant,
            producer="customer",
            action="customer.created",
            target_type="customer",
            target_id=uuid4(),
            actor=AuditActor(kind="HUMAN", user_id=uuid4()),
        )


def message(envelope, *, delivery=1):
    return SimpleNamespace(
        data=envelope.to_json(),
        subject="nxs.test.tenant.audit.intent.recorded",
        metadata=SimpleNamespace(num_delivered=delivery),
        ack=AsyncMock(),
        nak=AsyncMock(),
        term=AsyncMock(),
    )


def consumer(database, settings, *, handler=consume_intent):
    transport = SimpleNamespace(
        publish=AsyncMock(),
        dead_letter_subject=lambda **kwargs: "nxs.test.dlq.audit",
    )
    instance = AuditConsumer(
        ConsumerSpec(
            name="audit-delivery-test",
            subject_filter="nxs.test.tenant.audit.>",
            handler=handler,
            event_types=frozenset({AuditIntent.EVENT_TYPE}),
            max_delivery_attempts=3,
        ),
        database=database,
        transport=transport,
        dead_letters=DeadLetterRepository(),
        receipts=AuditReceiptStore(),
        settings=settings.events,
    )
    instance._log = SimpleNamespace(ainfo=AsyncMock(), awarning=AsyncMock(), aerror=AsyncMock())
    return instance


async def test_retry_rolls_back_fact_and_receipt_before_ack(
    tenant_database,
    make_organization,
    integration_env,
):
    organization = await make_organization()
    envelope = await source(tenant_database, organization.id)
    calls = 0

    async def fail_once(context):
        nonlocal calls
        calls += 1
        await consume_intent(context)
        if calls == 1:
            raise HandlerRetryableError("do-not-log-secret")

    instance = consumer(tenant_database, integration_env(), handler=fail_once)
    delivery = message(envelope)
    await instance._process(delivery)
    delivery.ack.assert_not_awaited()
    delivery.term.assert_not_awaited()
    assert delivery.nak.call_args.kwargs["delay"] > 0
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        assert await tenant.session.scalar(select(AuditRecord)) is None
        assert (
            await tenant.session.scalar(
                select(ConsumerReceiptRecord).where(
                    ConsumerReceiptRecord.consumer_name == "audit-delivery-test",
                    ConsumerReceiptRecord.event_id == envelope.event_id,
                )
            )
            is None
        )
    retry = message(envelope, delivery=2)
    await instance._process(retry)
    retry.ack.assert_awaited_once()
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        assert await tenant.session.scalar(select(AuditRecord)) is not None
        assert (
            await tenant.session.scalar(
                select(ConsumerReceiptRecord).where(
                    ConsumerReceiptRecord.consumer_name == "audit-delivery-test",
                    ConsumerReceiptRecord.event_id == envelope.event_id,
                )
            )
        ).status == "PROCESSED"


async def test_database_outage_never_acks_or_logs_untrusted_fields(
    tenant_database,
    make_organization,
    integration_env,
    monkeypatch,
):
    organization = await make_organization()
    envelope = await source(tenant_database, organization.id)
    envelope = envelope.model_copy(
        update={"producer": "secret-producer", "correlation_id": "secret-correlation"}
    )
    instance = consumer(tenant_database, integration_env())

    @asynccontextmanager
    async def unavailable(*args, **kwargs):
        raise ConnectionError("secret-database-connection")
        yield

    monkeypatch.setattr(instance, "_open", unavailable)
    delivery = message(envelope)
    await instance._process(delivery)
    delivery.ack.assert_not_awaited()
    delivery.term.assert_not_awaited()
    assert delivery.nak.call_args.kwargs["delay"] > 0
    assert "secret" not in str(instance._log.awarning.call_args_list)


async def test_poison_is_sanitized_in_storage_and_publication(
    tenant_database,
    make_organization,
    integration_env,
):
    organization = await make_organization()
    envelope = await source(tenant_database, organization.id)
    envelope = envelope.model_copy(
        update={
            "payload": {**envelope.payload, "token": "secret-token"},
            "producer": "secret-producer",
        }
    )
    instance = consumer(tenant_database, integration_env())
    delivery = message(envelope)
    await instance._process(delivery)
    delivery.ack.assert_not_awaited()
    delivery.nak.assert_not_awaited()
    delivery.term.assert_awaited_once()
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        stored = await tenant.session.scalar(select(EventDeadLetterRecord))
        assert stored.envelope["payload"] == {}
        assert "secret" not in str(stored.envelope)
        assert "secret" not in stored.error_summary
        assert await tenant.session.scalar(select(AuditRecord)) is None
    assert "secret" not in str(instance._transport.publish.call_args_list)
    assert "secret" not in str(instance._log.aerror.call_args_list)


async def test_malformed_subject_and_payload_never_logged(tenant_database, integration_env):
    instance = consumer(tenant_database, integration_env())
    delivery = SimpleNamespace(
        data=b"secret-payload",
        subject="secret-subject",
        term=AsyncMock(),
        ack=AsyncMock(),
        nak=AsyncMock(),
    )
    await instance._process(delivery)
    delivery.term.assert_awaited_once()
    delivery.ack.assert_not_awaited()
    delivery.nak.assert_not_awaited()
    assert "secret" not in str(instance._log.aerror.call_args_list)


async def test_retry_budget_terminates_without_a_success_fact(
    tenant_database,
    make_organization,
    integration_env,
):
    organization = await make_organization()
    envelope = await source(tenant_database, organization.id)

    async def always_fail(context):
        await consume_intent(context)
        raise HandlerRetryableError("secret-retry-exhaustion")

    instance = consumer(tenant_database, integration_env(), handler=always_fail)
    delivery = message(envelope, delivery=3)
    await instance._process(delivery)
    delivery.term.assert_awaited_once()
    delivery.ack.assert_not_awaited()
    delivery.nak.assert_not_awaited()
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        assert await tenant.session.scalar(select(AuditRecord)) is None
        assert (
            await tenant.session.scalar(
                select(ConsumerReceiptRecord).where(
                    ConsumerReceiptRecord.consumer_name == "audit-delivery-test",
                    ConsumerReceiptRecord.event_id == envelope.event_id,
                )
            )
        ).status == "DEAD"
        stored = await tenant.session.scalar(select(EventDeadLetterRecord))
        assert stored.attempt_count == 3
        assert "secret" not in str(stored.envelope)


async def test_terminal_storage_outage_delays_without_ack_or_term(
    tenant_database,
    make_organization,
    integration_env,
    monkeypatch,
):
    organization = await make_organization()
    envelope = await source(tenant_database, organization.id)
    instance = consumer(tenant_database, integration_env())

    @asynccontextmanager
    async def unavailable(*args, **kwargs):
        raise ConnectionError("secret-connection")
        yield

    monkeypatch.setattr(instance, "_open", unavailable)
    delivery = message(envelope, delivery=3)
    await instance._process(delivery)
    delivery.ack.assert_not_awaited()
    delivery.term.assert_not_awaited()
    assert delivery.nak.call_args.kwargs["delay"] == instance._settings.retry_max_delay_seconds
    assert "secret" not in str(instance._log.aerror.call_args_list)
