"""Event platform concurrency (NXS-EVENT-003, NXS-EVENT-006, section 8, section 20 N)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.events.consumer import ConsumerSpec, EventContext
from nexus_ai.events.publisher import OutboxRelay

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_many_relay_workers_publish_each_row_once(
    event_platform: Any,
    tenant_database: Any,
    nats_messaging: Any,
    make_organization: Any,
    make_tenant_event: Any,
) -> None:
    org = await make_organization()
    total = 40
    for _ in range(total):
        async with tenant_database.tenant_transaction(org.id) as ts:
            await event_platform.publisher.enqueue(ts.session, make_tenant_event(org.id))

    workers = [
        OutboxRelay(
            tenant_database,
            event_platform.transport,
            event_platform.outbox,
            event_platform.dead_letters,
            event_platform._events,
            worker_name=f"race-{i}",
        )
        for i in range(6)
    ]
    await asyncio.gather(*(w.drain_now() for w in workers))

    async with tenant_database.transaction() as session:
        rows = (
            await session.execute(text("SELECT status, count(*) FROM event_outbox GROUP BY status"))
        ).all()
    by_status = {r[0]: r[1] for r in rows}
    assert by_status.get("PUBLISHED") == total
    assert by_status.get("PENDING", 0) == 0
    assert by_status.get("PUBLISHING", 0) == 0
    # The outbox guarantees each ROW is marked PUBLISHED exactly once even though six
    # workers raced over the same population (FOR UPDATE SKIP LOCKED + leases).


async def test_concurrent_duplicate_deliveries_resolve_to_one_effect(
    event_platform: Any,
    tenant_database: Any,
    nats_messaging: Any,
    make_organization: Any,
    make_tenant_event: Any,
) -> None:
    org = await make_organization()
    effects: list[uuid.UUID] = []
    barrier = asyncio.Event()

    async def handler(ctx: EventContext) -> None:
        await barrier.wait()  # force both deliveries to race inside the handler
        effects.append(ctx.envelope.event_id)

    consumer = event_platform.register_consumer(
        ConsumerSpec(
            name=f"dup-{uuid.uuid4().hex[:8]}",
            subject_filter="nxs.test.tenant.>",
            handler=handler,
            event_types=frozenset({"platform.tenant_probe.emitted"}),
        )
    )
    envelope = make_tenant_event(org.id)
    subject = event_platform.publisher.subject_for(envelope)
    js = nats_messaging.jetstream()
    await js.publish(subject, envelope.to_json(), headers={"Nats-Msg-Id": "a"})
    await js.publish(subject, envelope.to_json(), headers={"Nats-Msg-Id": "b"})

    async def release() -> None:
        await asyncio.sleep(0.3)
        barrier.set()

    await asyncio.gather(consumer.drain_pending(timeout=3), release())
    assert effects == [envelope.event_id]


async def test_tenant_scope_isolation_across_concurrent_handlers(
    event_platform: Any,
    tenant_database: Any,
    nats_messaging: Any,
    make_organization: Any,
    make_tenant_event: Any,
) -> None:
    orgs = [await make_organization() for _ in range(4)]
    observed: dict[uuid.UUID, int] = {}

    async def handler(ctx: EventContext) -> None:
        await asyncio.sleep(0.05)
        count = (await ctx.session.execute(text("SELECT count(*) FROM organizations"))).scalar_one()
        observed[ctx.envelope.event_id] = int(count)

    consumer = event_platform.register_consumer(
        ConsumerSpec(
            name=f"iso-{uuid.uuid4().hex[:8]}",
            subject_filter="nxs.test.tenant.>",
            handler=handler,
            event_types=frozenset({"platform.tenant_probe.emitted"}),
        )
    )
    js = nats_messaging.jetstream()
    for org in orgs:
        env = make_tenant_event(org.id)
        await js.publish(
            event_platform.publisher.subject_for(env),
            env.to_json(),
            headers={"Nats-Msg-Id": uuid.uuid4().hex},
        )

    await consumer.drain_pending(timeout=3)
    # Every concurrent handler saw exactly its own single Organization — no cross-talk.
    assert set(observed.values()) == {1}
    assert len(observed) == 4
