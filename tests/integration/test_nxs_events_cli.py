"""Controlled dead-letter inspection and replay CLI (NXS-EVENT-007, section 13)."""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from sqlalchemy import text

from scripts.nxs_events.__main__ import _list, _replay

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.fixture(autouse=True)
def _cli_env(integration_env: Any) -> None:
    integration_env(NXS_EVENTS__PUBLISHER_ENABLED="false", NXS_EVENTS__CONSUMERS_ENABLED="false")
    # _list/_replay build Settings() from the environment directly.
    os.environ["NXS_EVENTS__PUBLISHER_ENABLED"] = "false"


async def _dead_letter_id(database: Any, event_id: Any) -> str:
    async with database.transaction() as session:
        return str(
            (
                await session.execute(
                    text("SELECT id FROM event_dead_letters WHERE event_id = :e"),
                    {"e": event_id},
                )
            ).scalar_one()
        )


async def test_list_reports_pending_dead_letters(
    event_platform: Any,
    tenant_database: Any,
    nats_messaging: Any,
    make_organization: Any,
    make_tenant_event: Any,
    capsys: Any,
) -> None:
    from nexus_ai.events.consumer import ConsumerSpec, EventContext
    from nexus_ai.events.errors import HandlerTerminalError

    org = await make_organization()

    async def handler(ctx: EventContext) -> None:
        raise HandlerTerminalError("poison")

    consumer = event_platform.register_consumer(
        ConsumerSpec(
            name="cli-c",
            subject_filter="nxs.test.tenant.>",
            handler=handler,
            event_types=frozenset({"platform.tenant_probe.emitted"}),
        )
    )
    envelope = make_tenant_event(org.id)
    await nats_messaging.jetstream().publish(
        event_platform.publisher.subject_for(envelope),
        envelope.to_json(),
        headers={"Nats-Msg-Id": "cli-1"},
    )
    await consumer.drain_pending()

    capsys.readouterr()  # discard setup / log output
    rc = await _list(50)
    assert rc == 0
    printed = capsys.readouterr().out
    out = json.loads(printed[printed.index("{") :])
    ids = {row["event_id"] for row in out["pending_dead_letters"]}
    assert str(envelope.event_id) in ids


async def test_replay_re_arms_an_outbox_dead_letter(
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

    dead_id = await _dead_letter_id(tenant_database, envelope.event_id)
    rc = await _replay(dead_id)
    assert rc == 0

    async with tenant_database.transaction() as session:
        status = (
            await session.execute(
                text("SELECT status FROM event_outbox WHERE id = :i"), {"i": envelope.event_id}
            )
        ).scalar_one()
        replayed = (
            await session.execute(
                text("SELECT replayed_at FROM event_dead_letters WHERE id = :i"), {"i": dead_id}
            )
        ).scalar_one()
    assert status == "PENDING"
    assert replayed is not None
    # It can now be published again.
    assert await event_platform.relay.drain_now() == 1


async def test_replay_rejects_an_unknown_id(event_platform: Any) -> None:
    rc = await _replay("00000000-0000-0000-0000-000000000000")
    assert rc == 2


async def test_replay_re_arms_a_consumer_dead_letter(
    event_platform: Any,
    tenant_database: Any,
    nats_messaging: Any,
    make_organization: Any,
    make_tenant_event: Any,
) -> None:
    from nexus_ai.events.consumer import ConsumerSpec, EventContext
    from nexus_ai.events.errors import HandlerTerminalError

    org = await make_organization()
    calls = {"n": 0}

    async def handler(ctx: EventContext) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise HandlerTerminalError("poison once")

    consumer = event_platform.register_consumer(
        ConsumerSpec(
            name="cli-consumer",
            subject_filter="nxs.test.tenant.>",
            handler=handler,
            event_types=frozenset({"platform.tenant_probe.emitted"}),
        )
    )
    envelope = make_tenant_event(org.id)
    subject = event_platform.publisher.subject_for(envelope)
    await nats_messaging.jetstream().publish(
        subject, envelope.to_json(), headers={"Nats-Msg-Id": "c-1"}
    )
    await consumer.drain_pending()
    assert calls["n"] == 1

    dead_id = await _dead_letter_id(tenant_database, envelope.event_id)
    assert await _replay(dead_id) == 0

    async with tenant_database.transaction() as session:
        status = (
            await session.execute(
                text("SELECT status FROM consumer_receipts WHERE event_id = :e"),
                {"e": envelope.event_id},
            )
        ).scalar_one()
    assert status == "PROCESSING"  # re-armed so the event can be reprocessed
    await consumer.drain_pending()
    assert calls["n"] == 2
