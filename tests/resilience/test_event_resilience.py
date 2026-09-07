"""Event platform resilience (section 20 A/B/Q/R, NXS-EVENT-004)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import text

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _status(database: Any, event_id: Any) -> str:
    async with database.transaction() as session:
        return (
            await session.execute(
                text("SELECT status FROM event_outbox WHERE id = :i"), {"i": event_id}
            )
        ).scalar_one()


async def test_relay_recovers_after_nats_reconnect(
    event_platform: Any,
    tenant_database: Any,
    nats_messaging: Any,
    make_organization: Any,
    make_tenant_event: Any,
) -> None:
    org = await make_organization()
    envelopes = [make_tenant_event(org.id) for _ in range(5)]
    for envelope in envelopes:
        async with tenant_database.tenant_transaction(org.id) as ts:
            await event_platform.publisher.enqueue(ts.session, envelope)

    await nats_messaging.disconnect()
    await event_platform.relay.run_once()  # every publish fails, nothing lost
    for envelope in envelopes:
        assert await _status(tenant_database, envelope.event_id) != "PUBLISHED"

    await nats_messaging.connect()
    for _ in range(30):
        await event_platform.relay.drain_now()
        statuses = [await _status(tenant_database, e.event_id) for e in envelopes]
        if all(status == "PUBLISHED" for status in statuses):
            break
        await asyncio.sleep(0.1)
    for envelope in envelopes:
        assert await _status(tenant_database, envelope.event_id) == "PUBLISHED"


async def test_shutdown_during_publishing_leaves_no_stuck_rows(
    event_platform: Any,
    tenant_database: Any,
    nats_messaging: Any,
    make_organization: Any,
    make_tenant_event: Any,
) -> None:
    org = await make_organization()
    for _ in range(10):
        async with tenant_database.tenant_transaction(org.id) as ts:
            await event_platform.publisher.enqueue(ts.session, make_tenant_event(org.id))

    # A worker claims a batch and then "crashes" (never marks the rows).
    async with tenant_database.transaction() as session:
        claimed = await event_platform.outbox.claim_batch(
            session, owner="crashed", batch_size=10, lease_seconds=1
        )
    assert claimed
    await asyncio.sleep(1.2)  # the lease expires

    # A healthy worker reclaims and drains everything — no permanently stuck row.
    published = 0
    for _ in range(20):
        published += await event_platform.relay.drain_now()
        async with tenant_database.transaction() as session:
            remaining = await event_platform.outbox.pending_count(session)
        if remaining == 0:
            break
        await asyncio.sleep(0.15)
    async with tenant_database.transaction() as session:
        assert await event_platform.outbox.pending_count(session) == 0


async def test_consumer_drains_in_flight_work_on_stop(
    event_platform: Any,
    tenant_database: Any,
    nats_messaging: Any,
    make_organization: Any,
    make_tenant_event: Any,
) -> None:
    from nexus_ai.events.consumer import ConsumerSpec, EventContext

    org = await make_organization()
    processed: list[uuid.UUID] = []

    async def handler(ctx: EventContext) -> None:
        await asyncio.sleep(0.2)
        processed.append(ctx.envelope.event_id)

    consumer = event_platform.register_consumer(
        ConsumerSpec(
            name=f"drain-{uuid.uuid4().hex[:8]}",
            subject_filter="nxs.test.tenant.>",
            handler=handler,
            event_types=frozenset({"platform.tenant_probe.emitted"}),
        )
    )
    envelope = make_tenant_event(org.id)
    await nats_messaging.jetstream().publish(
        event_platform.publisher.subject_for(envelope),
        envelope.to_json(),
        headers={"Nats-Msg-Id": "x"},
    )
    await consumer.start()
    await asyncio.sleep(0.3)
    await consumer.stop(timeout=5)
    assert processed == [envelope.event_id]
