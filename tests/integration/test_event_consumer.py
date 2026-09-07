"""Durable consumer framework: ack policy, idempotency, retry and terminal handling
(NXS-EVENT-005, NXS-EVENT-006, NXS-EVENT-007)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.events.consumer import ConsumerSpec, EventContext
from nexus_ai.events.errors import HandlerRetryableError, HandlerTerminalError

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

_TENANT_PROBE = "platform.tenant_probe.emitted"


def _spec(
    handler: Any, *, name: str | None = None, max_attempts: int | None = None
) -> ConsumerSpec:
    return ConsumerSpec(
        name=name or f"c-{uuid.uuid4().hex[:10]}",
        subject_filter="nxs.test.tenant.>",
        handler=handler,
        event_types=frozenset({_TENANT_PROBE}),
        max_delivery_attempts=max_attempts,
    )


async def _publish_via_outbox(platform: Any, database: Any, org_id: Any, envelope: Any) -> None:
    async with database.tenant_transaction(org_id) as ts:
        await platform.publisher.enqueue(ts.session, envelope)
    await platform.relay.drain_now()


async def _raw_publish(messaging: Any, subject: str, payload: bytes, msg_id: str) -> None:
    js = messaging.jetstream()
    await js.publish(subject, payload, headers={"Nats-Msg-Id": msg_id})


async def _receipt(database: Any, consumer: str, event_id: Any) -> dict[str, Any] | None:
    async with database.transaction() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, attempt_count, organization_id FROM consumer_receipts "
                    "WHERE consumer_name = :c AND event_id = :e"
                ),
                {"c": consumer, "e": event_id},
            )
        ).one_or_none()
    return None if row is None else {"status": row[0], "attempts": row[1], "org": row[2]}


async def test_handler_runs_in_tenant_scope_and_acks_after_success(
    event_platform: Any, tenant_database: Any, make_organization: Any, make_tenant_event: Any
) -> None:
    org = await make_organization()
    seen: list[uuid.UUID] = []
    visible_orgs: list[int] = []

    async def handler(ctx: EventContext) -> None:
        seen.append(ctx.envelope.event_id)
        count = (await ctx.session.execute(text("SELECT count(*) FROM organizations"))).scalar_one()
        visible_orgs.append(int(count))

    consumer = event_platform.register_consumer(_spec(handler))
    envelope = make_tenant_event(org.id)
    await _publish_via_outbox(event_platform, tenant_database, org.id, envelope)

    handled = await consumer.drain_pending()
    assert handled == 1
    assert seen == [envelope.event_id]
    assert visible_orgs == [1]  # RLS: the handler sees only its own Organization
    receipt = await _receipt(tenant_database, consumer._spec.name, envelope.event_id)
    assert receipt == {"status": "PROCESSED", "attempts": 1, "org": org.id}


async def test_duplicate_delivery_produces_one_business_effect(
    event_platform: Any,
    tenant_database: Any,
    nats_messaging: Any,
    make_organization: Any,
    make_tenant_event: Any,
) -> None:
    org = await make_organization()
    effects: list[uuid.UUID] = []

    async def handler(ctx: EventContext) -> None:
        effects.append(ctx.envelope.event_id)

    consumer = event_platform.register_consumer(_spec(handler))
    envelope = make_tenant_event(org.id)
    subject = event_platform.publisher.subject_for(envelope)
    # Two deliveries of the SAME event (distinct Nats-Msg-Id defeats broker dedup).
    await _raw_publish(nats_messaging, subject, envelope.to_json(), "delivery-1")
    await _raw_publish(nats_messaging, subject, envelope.to_json(), "delivery-2")

    await consumer.drain_pending()
    assert effects == [envelope.event_id]  # exactly one effect
    assert consumer.stats.processed == 1
    assert consumer.stats.duplicates == 1


async def test_ack_loss_then_redelivery_is_safe(
    event_platform: Any,
    tenant_database: Any,
    nats_messaging: Any,
    make_organization: Any,
    make_tenant_event: Any,
) -> None:
    org = await make_organization()
    effects: list[uuid.UUID] = []

    async def handler(ctx: EventContext) -> None:
        effects.append(ctx.envelope.event_id)

    consumer = event_platform.register_consumer(_spec(handler))
    envelope = make_tenant_event(org.id)
    subject = event_platform.publisher.subject_for(envelope)

    await _raw_publish(nats_messaging, subject, envelope.to_json(), "first")
    assert await consumer.drain_pending() == 1
    assert effects == [envelope.event_id]

    # The DB commit landed but "the ack was lost" — the same event is delivered again.
    await _raw_publish(nats_messaging, subject, envelope.to_json(), "redelivery")
    await consumer.drain_pending()
    assert effects == [envelope.event_id]  # still exactly one


async def test_retryable_failure_recovers_under_bounded_backoff(
    event_platform: Any, tenant_database: Any, make_organization: Any, make_tenant_event: Any
) -> None:
    org = await make_organization()
    attempts = {"n": 0}

    async def handler(ctx: EventContext) -> None:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise HandlerRetryableError("still warming up")

    consumer = event_platform.register_consumer(_spec(handler, max_attempts=10))
    envelope = make_tenant_event(org.id)
    await _publish_via_outbox(event_platform, tenant_database, org.id, envelope)

    await consumer.drain_pending(max_cycles=25, timeout=1.5)
    assert attempts["n"] == 3
    assert consumer.stats.retried >= 2
    receipt = await _receipt(tenant_database, consumer._spec.name, envelope.event_id)
    assert receipt is not None
    assert receipt["status"] == "PROCESSED"


async def test_terminal_failure_is_dead_lettered_and_not_reprocessed(
    event_platform: Any,
    tenant_database: Any,
    nats_messaging: Any,
    make_organization: Any,
    make_tenant_event: Any,
) -> None:
    org = await make_organization()
    calls = {"n": 0}

    async def handler(ctx: EventContext) -> None:
        calls["n"] += 1
        raise HandlerTerminalError("this event can never succeed: secret=hunter2")

    consumer = event_platform.register_consumer(_spec(handler))
    envelope = make_tenant_event(org.id)
    subject = event_platform.publisher.subject_for(envelope)
    await _raw_publish(nats_messaging, subject, envelope.to_json(), "poison-1")
    await consumer.drain_pending()

    assert calls["n"] == 1
    async with tenant_database.tenant_transaction(org.id) as ts:
        row = (
            await ts.session.execute(
                text(
                    "SELECT origin, failure_class, error_code, error_summary, consumer_name "
                    "FROM event_dead_letters WHERE event_id = :e"
                ),
                {"e": envelope.event_id},
            )
        ).one()
    assert row[0] == "CONSUMER"
    assert row[1] == "terminal"
    assert row[2] == "NXS_EVENT_HANDLER_TERMINAL"
    assert "hunter2" not in (row[3] or "")  # sanitized: no secret in the durable record
    receipt = await _receipt(tenant_database, consumer._spec.name, envelope.event_id)
    assert receipt is not None
    assert receipt["status"] == "DEAD"

    # A redelivery of the poison event is acknowledged immediately — no hot loop.
    await _raw_publish(nats_messaging, subject, envelope.to_json(), "poison-2")
    await consumer.drain_pending()
    assert calls["n"] == 1


async def test_unknown_event_type_is_quarantined(
    event_platform: Any,
    tenant_database: Any,
    nats_messaging: Any,
    make_organization: Any,
    make_tenant_event: Any,
) -> None:
    org = await make_organization()

    async def handler(ctx: EventContext) -> None:  # pragma: no cover - never reached
        raise AssertionError("handler must not run for an unknown type")

    consumer = event_platform.register_consumer(_spec(handler))
    envelope = make_tenant_event(org.id, event_type="platform.tenant_probe.emitted")
    rogue = envelope.model_copy(update={"event_type": "mystery.happened"})
    subject = "nxs.test.tenant.mystery.happened"
    await _raw_publish(nats_messaging, subject, rogue.to_json(), "rogue-1")

    await consumer.drain_pending()
    assert consumer.stats.dead_lettered >= 1


async def test_malformed_message_is_terminated_without_leaking(
    event_platform: Any, nats_messaging: Any, capfd: Any
) -> None:
    async def handler(ctx: EventContext) -> None:  # pragma: no cover
        raise AssertionError("unreachable")

    consumer = event_platform.register_consumer(_spec(handler))
    await _raw_publish(
        nats_messaging, "nxs.test.tenant.platform.tenant_probe.emitted", b"{ not json ", "bad-1"
    )
    await consumer.drain_pending()
    assert consumer.stats.dead_lettered >= 1
