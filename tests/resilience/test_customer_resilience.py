"""Customer/conversation resilience matrix (NXS-CUSTOMER-001).

Rollback behavior, duplicate retries, NATS outage, event replay and timeline dedup —
deterministic state in every failure mode.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.domain.customers.entities import (
    ActivityType,
    CreateConversationRequest,
    CreateCustomerRequest,
    IdentityType,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


def _create_request(**overrides: object) -> CreateCustomerRequest:
    payload: dict[str, object] = {
        "display_name": f"Res {uuid.uuid4().hex[:8]}",
        "identity_type": IdentityType.EMAIL,
        "identity_value": f"res-{uuid.uuid4().hex[:8]}@example.com",
        "identity_source": "test",
    }
    payload.update(overrides)
    return CreateCustomerRequest(**payload)


def _resources(client: Any) -> Any:
    return client.nexus_app.state.lifespan.resources  # type: ignore[attr-defined]


class TestCustomerResilience:
    async def test_outbox_failure_rolls_back_customer_creation(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        request = _create_request()

        original_enqueue = resources.event_platform.publisher.enqueue

        async def _failing(session: Any, envelope: Any) -> bool:
            raise RuntimeError("simulated outbox failure")

        resources.event_platform.publisher.enqueue = _failing  # type: ignore[method-assign]
        try:
            with pytest.raises(RuntimeError, match="simulated outbox failure"):
                await resources.customers.resolve_or_create(org.id, request)
        finally:
            resources.event_platform.publisher.enqueue = original_enqueue  # type: ignore[method-assign]

        # Nothing was committed: retry creates exactly one customer.
        async with resources.database.tenant_transaction(org.id) as tenant:
            count = (
                await tenant.session.execute(text("SELECT count(*) FROM customers"))
            ).scalar_one()
        assert count == 0
        customer, created = await resources.customers.resolve_or_create(org.id, request)
        assert created is True
        assert customer.id is not None

    async def test_duplicate_retry_after_response_loss(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        request = _create_request()
        first, _ = await resources.customers.resolve_or_create(org.id, request)
        # Response lost: the caller retries the identical request.
        for _ in range(3):
            replay, created = await resources.customers.resolve_or_create(org.id, request)
            assert created is False
            assert replay.id == first.id

    async def test_nats_outage_does_not_corrupt_customer_creation(
        self, auth_client: Any, make_auth_org: Any, nats_messaging: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        await nats_messaging.disconnect()
        try:
            customer, created = await resources.customers.resolve_or_create(
                org.id, _create_request()
            )
            assert created is True
        finally:
            await nats_messaging.connect()
        # The outbox row survives and the relay publishes on recovery.
        processed = await resources.event_platform.relay.drain_now()
        assert processed >= 1
        async with resources.database.tenant_transaction(org.id) as tenant:
            status = (
                await tenant.session.execute(
                    text("SELECT status FROM event_outbox WHERE event_type = 'customers.created'")
                )
            ).scalar_one()
        assert status == "PUBLISHED"

    async def test_timeline_dedup_on_replay(self, auth_client: Any, make_auth_org: Any) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        customer, _ = await resources.customers.resolve_or_create(org.id, _create_request())
        # Replaying the same business effect with the same dedup key appends nothing.
        # Each append is its own transaction: the replay's dedup conflict must not
        # abort the FIRST append (they commit independently).
        from nexus_ai.domain.customers.repository import ConversationActivityRepository

        for _ in range(2):
            async with resources.database.tenant_transaction(org.id) as tenant:
                await ConversationActivityRepository(tenant).append(
                    customer_id=customer.id,
                    conversation_id=None,
                    activity_type=ActivityType.MESSAGE_INBOUND,
                    dedup_key="replay-key-0001",
                    data={"content_type": "text"},
                )
        timeline = await resources.customers.timeline(org.id, customer.id, after=None, limit=50)
        inbound = [a for a in timeline if a.activity_type == "message.inbound"]
        assert len(inbound) == 1

    async def test_conversation_close_retry_is_stable(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        customer, _ = await resources.customers.resolve_or_create(org.id, _create_request())
        conversation, _ = await resources.conversations.open_or_resolve(
            org.id, CreateConversationRequest(customer_id=customer.id, channel="web")
        )
        for _ in range(3):
            closed = await resources.conversations.close(org.id, conversation.id)
            assert closed.status.value == "CLOSED"
