"""Customer / conversation concurrency matrix (NXS-CUSTOMER-001, race-recovery corrective).

Database constraints are the final authority — no Python locks. Every equivalent
concurrent caller must SUCCEED and converge on one canonical resource; only genuinely
contested-ownership operations produce a deterministic conflict. Tests never pass merely
because "at least one succeeded" — an unexpected exception fails the test.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.core.errors import ConversationCustomerConflictError, IdentityConflictError
from nexus_ai.domain.customers.entities import (
    CreateConversationRequest,
    CreateCustomerRequest,
    IdentityType,
    LinkIdentityRequest,
)
from nexus_ai.domain.customers.normalization import normalize_identity_value

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


def _create_request(**overrides: object) -> CreateCustomerRequest:
    payload: dict[str, object] = {
        "display_name": f"Race {uuid.uuid4().hex[:8]}",
        "identity_type": IdentityType.EMAIL,
        "identity_value": f"race-{uuid.uuid4().hex[:8]}@example.com",
        "identity_source": "test",
    }
    payload.update(overrides)
    return CreateCustomerRequest(**payload)


def _resources(client: Any) -> Any:
    return client.nexus_app.state.lifespan.resources  # type: ignore[attr-defined]


async def _scalar(resources: Any, org_id: Any, sql: str, params: dict[str, Any]) -> int:
    async with resources.database.tenant_transaction(org_id) as tenant:
        return int((await tenant.session.execute(text(sql), params)).scalar_one())


async def _split(coros: list[Any]) -> tuple[list[Any], list[BaseException]]:
    """Run every coroutine; return (successful results, exceptions). Never swallows."""
    results = await asyncio.gather(*coros, return_exceptions=True)
    ok = [r for r in results if not isinstance(r, BaseException)]
    errs = [r for r in results if isinstance(r, BaseException)]
    return ok, errs


class TestConcurrentCustomerResolveOrCreate:
    @pytest.mark.parametrize("n", [2, 10])
    async def test_identical_resolve_or_create_all_converge(
        self, auth_client: Any, make_auth_org: Any, n: int
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        request = _create_request()

        results = await asyncio.gather(
            *[resources.customers.resolve_or_create(org.id, request) for _ in range(n)]
        )

        assert len(results) == n
        customer_ids = {c.id for c, _ in results}
        assert len(customer_ids) == 1  # every caller converged on ONE Customer
        assert sum(1 for _, created in results if created) == 1  # exactly one creator
        canonical = customer_ids.pop()

        normalized = normalize_identity_value(request.identity_type, request.identity_value)
        assert (
            await _scalar(
                resources,
                org.id,
                "SELECT count(*) FROM customer_identities WHERE normalized_value = :v",
                {"v": normalized},
            )
            == 1
        )
        assert (
            await _scalar(
                resources,
                org.id,
                "SELECT count(*) FROM customers WHERE id = :id",
                {"id": canonical},
            )
            == 1
        )

    async def test_ten_identical_produce_exactly_one_event_and_timeline_effect(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        request = _create_request()

        results = await asyncio.gather(
            *[resources.customers.resolve_or_create(org.id, request) for _ in range(10)]
        )
        customer = results[0][0]

        # Exactly one domain mutation + event intent for the unique creation.
        assert (
            await _scalar(
                resources,
                org.id,
                "SELECT count(*) FROM event_outbox WHERE event_type = 'customers.created' "
                "AND (envelope->'payload'->>'customer_id') = :cid",
                {"cid": str(customer.id)},
            )
            == 1
        )
        assert (
            await _scalar(
                resources,
                org.id,
                "SELECT count(*) FROM conversation_activities "
                "WHERE customer_id = :cid AND activity_type = 'customer.created'",
                {"cid": customer.id},
            )
            == 1
        )
        assert (
            await _scalar(
                resources,
                org.id,
                "SELECT count(*) FROM conversation_activities "
                "WHERE customer_id = :cid AND activity_type = 'identity.linked'",
                {"cid": customer.id},
            )
            == 1
        )


class TestConcurrentLinkIdentity:
    async def test_same_customer_same_identity_all_converge(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        customer, _ = await resources.customers.resolve_or_create(org.id, _create_request())
        link = LinkIdentityRequest(
            identity_type=IdentityType.PHONE, identity_value="+506 1111 2222"
        )

        results = await asyncio.gather(
            *[resources.customers.link_identity(org.id, customer.id, link) for _ in range(8)]
        )

        assert len(results) == 8
        assert len({identity.id for identity in results}) == 1  # one identity row, all converge
        identities = await resources.customers.list_identities(org.id, customer.id)
        assert len(identities) == 2  # the EMAIL from creation + this PHONE, once

    async def test_different_customers_exactly_one_winner(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        customer_a, _ = await resources.customers.resolve_or_create(org.id, _create_request())
        customer_b, _ = await resources.customers.resolve_or_create(org.id, _create_request())
        customer_c, _ = await resources.customers.resolve_or_create(org.id, _create_request())
        contested = LinkIdentityRequest(
            identity_type=IdentityType.EMAIL,
            identity_value=f"contested-{uuid.uuid4().hex[:8]}@example.com",
        )

        ok, errs = await _split(
            [
                resources.customers.link_identity(org.id, cid, contested)
                for cid in (customer_a.id, customer_b.id, customer_c.id)
            ]
        )

        assert len(ok) == 1  # exactly one Customer owns the identity
        assert all(isinstance(e, IdentityConflictError) for e in errs)  # no generic failures
        assert len(errs) == 2
        winning_owner = ok[0].customer_id
        assert winning_owner in {customer_a.id, customer_b.id, customer_c.id}
        # The identity was never split across Customers.
        assert (
            await _scalar(
                resources,
                org.id,
                "SELECT count(DISTINCT customer_id) FROM customer_identities "
                "WHERE normalized_value = :v",
                {"v": normalize_identity_value(IdentityType.EMAIL, contested.identity_value)},
            )
            == 1
        )

    async def test_loser_of_same_customer_race_still_succeeds_idempotently(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        customer, _ = await resources.customers.resolve_or_create(org.id, _create_request())
        link = LinkIdentityRequest(
            identity_type=IdentityType.EMAIL,
            identity_value=f"shared-{uuid.uuid4().hex[:8]}@example.com",
        )
        # 6 identical concurrent links by the SAME customer: none may raise.
        results = await asyncio.gather(
            *[resources.customers.link_identity(org.id, customer.id, link) for _ in range(6)]
        )
        assert len({r.id for r in results}) == 1


class TestConcurrentOpenConversation:
    @pytest.mark.parametrize("n", [2, 5, 8])
    async def test_same_external_thread_all_converge(
        self, auth_client: Any, make_auth_org: Any, n: int
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        customer, _ = await resources.customers.resolve_or_create(org.id, _create_request())
        payload = CreateConversationRequest(
            customer_id=customer.id,
            channel="whatsapp",
            provider_namespace="wa",
            external_thread_id=f"race-thread-{uuid.uuid4().hex[:8]}",
        )

        results = await asyncio.gather(
            *[resources.conversations.open_or_resolve(org.id, payload) for _ in range(n)]
        )

        assert len(results) == n
        conversation_ids = {c.id for c, _ in results}
        assert len(conversation_ids) == 1
        assert sum(1 for _, created in results if created) == 1
        assert (
            await _scalar(
                resources,
                org.id,
                "SELECT count(*) FROM conversations WHERE provider_namespace = 'wa' "
                "AND external_thread_id = :t",
                {"t": payload.external_thread_id},
            )
            == 1
        )

    async def test_five_concurrent_produce_one_event_and_timeline_effect(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        customer, _ = await resources.customers.resolve_or_create(org.id, _create_request())
        payload = CreateConversationRequest(
            customer_id=customer.id,
            channel="email",
            provider_namespace="smtp",
            external_thread_id=f"race-thread-{uuid.uuid4().hex[:8]}",
        )
        results = await asyncio.gather(
            *[resources.conversations.open_or_resolve(org.id, payload) for _ in range(5)]
        )
        conversation = results[0][0]

        assert (
            await _scalar(
                resources,
                org.id,
                "SELECT count(*) FROM event_outbox WHERE event_type = 'conversations.opened' "
                "AND (envelope->'payload'->>'conversation_id') = :cid",
                {"cid": str(conversation.id)},
            )
            == 1
        )
        assert (
            await _scalar(
                resources,
                org.id,
                "SELECT count(*) FROM conversation_activities "
                "WHERE conversation_id = :cid AND activity_type = 'conversation.opened'",
                {"cid": conversation.id},
            )
            == 1
        )

    async def test_conflicting_customer_on_deterministic_thread_is_deterministic(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        customer_a, _ = await resources.customers.resolve_or_create(org.id, _create_request())
        customer_b, _ = await resources.customers.resolve_or_create(org.id, _create_request())
        thread = f"conflict-thread-{uuid.uuid4().hex[:8]}"
        request_a = CreateConversationRequest(
            customer_id=customer_a.id,
            channel="whatsapp",
            provider_namespace="wa",
            external_thread_id=thread,
        )
        request_b = request_a.model_copy(update={"customer_id": customer_b.id})

        ok, errs = await _split(
            [
                resources.conversations.open_or_resolve(org.id, request_a),
                resources.conversations.open_or_resolve(org.id, request_a),
                resources.conversations.open_or_resolve(org.id, request_b),
            ]
        )
        # A wins the thread; the second A-caller converges; the B-caller is a deterministic
        # customer conflict — B's Conversation is never silently returned.
        assert all(isinstance(e, ConversationCustomerConflictError) for e in errs)
        assert len(errs) == 1
        assert len(ok) == 2
        assert len({c.id for c, _ in ok}) == 1
        winner_id = ok[0][0].id

        # Re-running B in isolation is still a stable, deterministic conflict.
        with pytest.raises(ConversationCustomerConflictError):
            await resources.conversations.open_or_resolve(org.id, request_b)

        assert (
            await _scalar(
                resources,
                org.id,
                "SELECT count(*) FROM conversations WHERE id = :id AND customer_id::text = :cid",
                {"id": winner_id, "cid": str(customer_a.id)},
            )
            == 1
        )
