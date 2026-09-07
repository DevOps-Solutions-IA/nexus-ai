"""Event platform security matrix (NXS-EVENT-008, section 15, section 20 O/P)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import structlog
from sqlalchemy import text

from nexus_ai.events.consumer import ConsumerSpec, EventContext
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.errors import SubjectValidationError

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


def _spec(handler: Any) -> ConsumerSpec:
    return ConsumerSpec(
        name=f"sec-{uuid.uuid4().hex[:10]}",
        subject_filter="nxs.test.tenant.>",
        handler=handler,
        event_types=frozenset({"platform.tenant_probe.emitted"}),
    )


async def _raw_publish(messaging: Any, subject: str, payload: bytes) -> None:
    await messaging.jetstream().publish(subject, payload, headers={"Nats-Msg-Id": uuid.uuid4().hex})


async def test_publisher_never_accepts_a_caller_chosen_subject(
    event_platform: Any, make_organization: Any, make_tenant_event: Any
) -> None:
    org = await make_organization()
    envelope = make_tenant_event(org.id)
    # There is no API to publish to an arbitrary subject; the subject is derived from the
    # trusted envelope only, and a malformed event type cannot produce one.
    with pytest.raises((SubjectValidationError, ValueError)):
        EventEnvelope.create(
            event_type="tenant.>",
            event_version=1,
            aggregate_type="x",
            aggregate_id="y",
            producer="p",
            payload={},
            organization_id=org.id,
        )
    subject = event_platform.publisher.subject_for(envelope)
    assert subject == "nxs.test.tenant.platform.tenant_probe.emitted"
    assert "*" not in subject and ">" not in subject


async def test_forged_tenant_in_payload_is_rejected(
    event_platform: Any,
    tenant_database: Any,
    nats_messaging: Any,
    make_organization: Any,
) -> None:
    org_a = await make_organization()
    org_b = await make_organization()
    handled: list[uuid.UUID] = []

    async def handler(ctx: EventContext) -> None:  # pragma: no cover - must not run
        handled.append(ctx.envelope.event_id)

    consumer = event_platform.register_consumer(_spec(handler))
    # Envelope is authoritative for org A, but the payload smuggles org B.
    envelope = EventEnvelope.create(
        event_type="platform.tenant_probe.emitted",
        event_version=1,
        aggregate_type="platform",
        aggregate_id=uuid.uuid4().hex,
        producer="attacker",
        payload={"nonce": "x", "organization_id": str(org_b.id)},
        organization_id=org_a.id,
    )
    await _raw_publish(
        nats_messaging, "nxs.test.tenant.platform.tenant_probe.emitted", envelope.to_json()
    )
    await consumer.drain_pending()
    assert handled == []
    assert consumer.stats.dead_lettered >= 1


async def test_subject_scope_confusion_is_rejected(
    event_platform: Any, nats_messaging: Any, make_organization: Any, make_tenant_event: Any
) -> None:
    org = await make_organization()

    async def handler(ctx: EventContext) -> None:  # pragma: no cover
        raise AssertionError("unreachable")

    consumer = event_platform.register_consumer(_spec(handler))
    envelope = make_tenant_event(org.id)
    # A tenant envelope delivered on a GLOBAL-looking subject.
    await _raw_publish(
        nats_messaging, "nxs.test.global.platform.tenant_probe.emitted", envelope.to_json()
    )
    # The consumer only subscribes to nxs.test.tenant.> so it never sees a global subject;
    # publish the confusion directly onto its filter with a mismatched envelope instead.
    bad = envelope.model_copy(update={"aggregate_id": "z"})
    await _raw_publish(nats_messaging, "nxs.test.tenant.auth.something", bad.to_json())
    await consumer.drain_pending()
    assert consumer.stats.dead_lettered >= 1


async def test_runtime_role_cannot_read_another_tenants_outbox(
    event_platform: Any,
    tenant_database: Any,
    raw_runtime_connection: Any,
    make_organization: Any,
    make_tenant_event: Any,
) -> None:
    org_a = await make_organization()
    org_b = await make_organization()
    async with tenant_database.tenant_transaction(org_a.id) as ts:
        await event_platform.publisher.enqueue(ts.session, make_tenant_event(org_a.id))

    # Bind org B's scope on a raw runtime connection and try to see A's outbox row.
    await raw_runtime_connection.execute(
        "SELECT set_config('nxs.organization_id', $1, false)", str(org_b.id)
    )
    rows = await raw_runtime_connection.fetch("SELECT organization_id FROM event_outbox")
    assert all(r["organization_id"] == org_b.id for r in rows)
    assert org_a.id not in {r["organization_id"] for r in rows}


async def test_runtime_role_cannot_delete_event_rows(raw_runtime_connection: Any) -> None:
    import asyncpg

    for table in ("event_outbox", "event_dead_letters", "consumer_receipts"):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await raw_runtime_connection.execute(f"DELETE FROM {table}")  # noqa: S608 - literal


async def test_terminal_failure_does_not_log_the_exception_message(
    event_platform: Any,
    tenant_database: Any,
    nats_messaging: Any,
    make_organization: Any,
    make_tenant_event: Any,
) -> None:
    org = await make_organization()

    leak_marker = "SENSITIVE-DETAIL-THAT-MUST-NOT-PROPAGATE"

    async def handler(ctx: EventContext) -> None:
        raise RuntimeError(f"handler blew up: {leak_marker}")

    consumer = event_platform.register_consumer(_spec(handler))
    envelope = make_tenant_event(org.id)
    await _raw_publish(
        nats_messaging, "nxs.test.tenant.platform.tenant_probe.emitted", envelope.to_json()
    )
    with structlog.testing.capture_logs() as logs:
        await consumer.drain_pending()
    rendered = str(logs)
    assert leak_marker not in rendered
    assert "handler blew up" not in rendered

    async with tenant_database.tenant_transaction(org.id) as ts:
        summary = (
            await ts.session.execute(
                text("SELECT error_summary FROM event_dead_letters WHERE event_id = :e"),
                {"e": envelope.event_id},
            )
        ).scalar_one()
    assert leak_marker not in (summary or "")
