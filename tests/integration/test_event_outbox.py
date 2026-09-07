"""Transactional outbox: atomicity, tenant safety and safe concurrent claiming
(NXS-EVENT-003, NXS-EVENT-008)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.events.errors import EventTenantScopeError

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _pending(tenant_database: Any, platform: Any) -> int:
    async with tenant_database.transaction() as session:
        return await platform.outbox.pending_count(session)


async def test_enqueue_and_business_mutation_commit_atomically(
    event_platform: Any, tenant_database: Any, make_organization: Any, make_tenant_event: Any
) -> None:
    org = await make_organization()
    envelope = make_tenant_event(org.id)
    async with tenant_database.tenant_transaction(org.id) as ts:
        inserted = await event_platform.publisher.enqueue(ts.session, envelope)
        assert inserted is True
    assert await _pending(tenant_database, event_platform) == 1


async def test_no_event_from_uncommitted_state(
    event_platform: Any, tenant_database: Any, make_organization: Any, make_tenant_event: Any
) -> None:
    org = await make_organization()
    with pytest.raises(RuntimeError, match="business failure"):
        async with tenant_database.tenant_transaction(org.id) as ts:
            await event_platform.publisher.enqueue(ts.session, make_tenant_event(org.id))
            raise RuntimeError("business failure")
    assert await _pending(tenant_database, event_platform) == 0


async def test_enqueue_is_idempotent_on_event_id(
    event_platform: Any, tenant_database: Any, make_organization: Any, make_tenant_event: Any
) -> None:
    org = await make_organization()
    envelope = make_tenant_event(org.id)
    async with tenant_database.tenant_transaction(org.id) as ts:
        assert await event_platform.publisher.enqueue(ts.session, envelope) is True
    async with tenant_database.tenant_transaction(org.id) as ts:
        assert await event_platform.publisher.enqueue(ts.session, envelope) is False
    assert await _pending(tenant_database, event_platform) == 1


async def test_forged_tenant_enqueue_is_rejected_by_rls(
    event_platform: Any, tenant_database: Any, make_organization: Any, make_tenant_event: Any
) -> None:
    org_a = await make_organization()
    org_b = await make_organization()
    async with tenant_database.tenant_transaction(org_a.id) as ts:
        with pytest.raises(EventTenantScopeError):
            await event_platform.publisher.enqueue(ts.session, make_tenant_event(org_b.id))
    assert await _pending(tenant_database, event_platform) == 0


async def test_claim_batch_leases_rows_and_skips_locked(
    event_platform: Any, tenant_database: Any, make_organization: Any, make_tenant_event: Any
) -> None:
    org = await make_organization()
    for _ in range(3):
        async with tenant_database.tenant_transaction(org.id) as ts:
            await event_platform.publisher.enqueue(ts.session, make_tenant_event(org.id))

    async with tenant_database.transaction() as session:
        claimed = await event_platform.outbox.claim_batch(
            session, owner="w1", batch_size=10, lease_seconds=30
        )
    assert len(claimed) == 3
    assert all(item.attempt_count == 1 for item in claimed)

    async with tenant_database.transaction() as session:
        again = await event_platform.outbox.claim_batch(
            session, owner="w2", batch_size=10, lease_seconds=30
        )
    assert again == []


async def test_expired_lease_is_reclaimed(
    event_platform: Any, tenant_database: Any, make_organization: Any, make_tenant_event: Any
) -> None:
    org = await make_organization()
    async with tenant_database.tenant_transaction(org.id) as ts:
        await event_platform.publisher.enqueue(ts.session, make_tenant_event(org.id))
    async with tenant_database.transaction() as session:
        first = await event_platform.outbox.claim_batch(
            session, owner="crashed", batch_size=10, lease_seconds=30
        )
        assert len(first) == 1
    # Simulate the worker crashing: force the lease into the past.
    async with tenant_database.transaction() as session:
        await session.execute(
            text("UPDATE event_outbox SET lease_expires_at = :past WHERE status = 'PUBLISHING'"),
            {"past": dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)},
        )
    async with tenant_database.transaction() as session:
        recovered = await event_platform.outbox.claim_batch(
            session, owner="w2", batch_size=10, lease_seconds=30
        )
    assert len(recovered) == 1
    assert recovered[0].attempt_count == 2


async def test_mark_published_and_reschedule(
    event_platform: Any, tenant_database: Any, make_organization: Any, make_tenant_event: Any
) -> None:
    org = await make_organization()
    async with tenant_database.tenant_transaction(org.id) as ts:
        await event_platform.publisher.enqueue(ts.session, make_tenant_event(org.id))
    async with tenant_database.transaction() as session:
        [item] = await event_platform.outbox.claim_batch(
            session, owner="w", batch_size=1, lease_seconds=30
        )
    async with tenant_database.transaction() as session:
        await event_platform.outbox.mark_published(session, item.id)
    async with tenant_database.transaction() as session:
        row = (
            await session.execute(
                text("SELECT status, published_at FROM event_outbox WHERE id = :i"),
                {"i": item.id},
            )
        ).one()
    assert row[0] == "PUBLISHED"
    assert row[1] is not None
    assert await _pending(tenant_database, event_platform) == 0
