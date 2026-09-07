"""JetStream publisher and outbox relay: confirmed publication, dedup, outage survival
(NXS-EVENT-003, NXS-EVENT-004)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.errors import EventTenantScopeError

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _status(database: Any, outbox_id: Any) -> str:
    async with database.transaction() as session:
        return (
            await session.execute(
                text("SELECT status FROM event_outbox WHERE id = :i"), {"i": outbox_id}
            )
        ).scalar_one()


async def test_relay_publishes_and_marks_published(
    event_platform: Any,
    tenant_database: Any,
    nats_messaging: Any,
    make_organization: Any,
    make_tenant_event: Any,
) -> None:
    org = await make_organization()
    envelope = make_tenant_event(org.id, nonce="published-check")
    async with tenant_database.tenant_transaction(org.id) as ts:
        await event_platform.publisher.enqueue(ts.session, envelope)

    processed = await event_platform.relay.drain_now()
    assert processed == 1
    assert await _status(tenant_database, envelope.event_id) == "PUBLISHED"

    # The message is really on the stream and carries the canonical envelope.
    js = nats_messaging.jetstream()
    psub = await js.pull_subscribe(
        event_platform.transport.tenant_subject_filter(),
        durable=f"verify-{uuid.uuid4().hex[:8]}",
        stream="NXS_EVENTS",
    )
    [msg] = await psub.fetch(1, timeout=5)
    delivered = EventEnvelope.from_json(msg.data)
    assert delivered.event_id == envelope.event_id
    assert msg.headers["Nats-Msg-Id"] == str(envelope.event_id)
    await msg.ack()


async def test_duplicate_publication_is_detected_by_the_broker(
    event_platform: Any, nats_messaging: Any, make_organization: Any, make_tenant_event: Any
) -> None:
    org = await make_organization()
    envelope = make_tenant_event(org.id)
    subject = event_platform.publisher.subject_for(envelope)
    first = await event_platform.transport.publish(
        subject, envelope.to_json(), headers=envelope.nats_headers()
    )
    second = await event_platform.transport.publish(
        subject, envelope.to_json(), headers=envelope.nats_headers()
    )
    assert first.duplicate is False
    assert second.duplicate is True


async def test_global_event_publishes_directly(event_platform: Any) -> None:
    receipt = await event_platform.emit_probe(note="lifecycle-check")
    assert receipt.stream == "NXS_EVENTS"
    assert receipt.sequence >= 1


async def test_publish_global_rejects_a_tenant_event(
    event_platform: Any, make_organization: Any, make_tenant_event: Any
) -> None:
    org = await make_organization()
    with pytest.raises(EventTenantScopeError):
        await event_platform.publisher.publish_global(make_tenant_event(org.id))


async def test_committed_events_survive_a_nats_outage(
    event_platform: Any,
    tenant_database: Any,
    nats_messaging: Any,
    make_organization: Any,
    make_tenant_event: Any,
) -> None:
    org = await make_organization()
    envelope = make_tenant_event(org.id, nonce="outage-survivor")
    async with tenant_database.tenant_transaction(org.id) as ts:
        await event_platform.publisher.enqueue(ts.session, envelope)

    # NATS goes away before the relay runs.
    await nats_messaging.disconnect()
    await event_platform.relay.run_once()
    status = await _status(tenant_database, envelope.event_id)
    assert status in {"PENDING", "PUBLISHING", "FAILED"}  # never lost, never PUBLISHED

    # NATS comes back; the committed row is delivered once its backoff elapses.
    await nats_messaging.connect()
    import asyncio

    for _ in range(20):
        await event_platform.relay.drain_now()
        if await _status(tenant_database, envelope.event_id) == "PUBLISHED":
            break
        await asyncio.sleep(0.1)
    assert await _status(tenant_database, envelope.event_id) == "PUBLISHED"


async def test_publication_is_dead_lettered_after_exhausting_attempts(
    event_platform: Any,
    tenant_database: Any,
    nats_messaging: Any,
    make_organization: Any,
    make_tenant_event: Any,
) -> None:
    org = await make_organization()
    envelope = make_tenant_event(org.id)
    async with tenant_database.tenant_transaction(org.id) as ts:
        await event_platform.publisher.enqueue(ts.session, envelope)
    # Force the row to the last allowed attempt; the next failed publish dead-letters it.
    async with tenant_database.transaction() as session:
        await session.execute(
            text("UPDATE event_outbox SET attempt_count = 8 WHERE id = :i"),
            {"i": envelope.event_id},
        )
        [item] = await event_platform.outbox.claim_batch(
            session, owner="w", batch_size=1, lease_seconds=30
        )

    await nats_messaging.disconnect()
    try:
        await event_platform.relay._publish_one(item)
    finally:
        await nats_messaging.connect()

    async with tenant_database.tenant_transaction(org.id) as ts:
        dead = (
            await ts.session.execute(
                text("SELECT origin, error_code FROM event_dead_letters WHERE event_id = :e"),
                {"e": envelope.event_id},
            )
        ).one_or_none()
    assert dead is not None
    assert dead[0] == "OUTBOX_PUBLISH"
    assert await _status(tenant_database, envelope.event_id) == "DEAD"
