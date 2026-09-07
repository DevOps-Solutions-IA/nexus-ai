"""Customer/conversation concurrency matrix (NXS-CUSTOMER-001).

Database constraints are the final authority — no Python locks. Ten simultaneous
resolve-or-create calls yield one Customer; identity-linking races yield one row;
external-thread races yield one Conversation.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.core.errors import IdentityConflictError
from nexus_ai.domain.customers.entities import (
    CreateConversationRequest,
    CreateCustomerRequest,
    IdentityType,
    LinkIdentityRequest,
)

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


async def _customer_count(resources: Any, org_id: Any, identity_value: str) -> int:
    from nexus_ai.domain.customers.normalization import normalize_identity_value

    normalized = normalize_identity_value(IdentityType.EMAIL, identity_value)
    async with resources.database.tenant_transaction(org_id) as tenant:
        return (
            await tenant.session.execute(
                text("SELECT count(*) FROM customer_identities WHERE normalized_value = :v"),
                {"v": normalized},
            )
        ).scalar_one()


class TestConcurrentCustomerCreation:
    async def test_ten_simultaneous_resolve_or_create_one_customer(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        request = _create_request()

        async def _create() -> Any:
            try:
                return await resources.customers.resolve_or_create(org.id, request)
            except Exception as exc:
                return exc

        results = await asyncio.gather(*[_create() for _ in range(10)])
        customers = [r[0] for r in results if isinstance(r, tuple)]
        assert len({c.id for c in customers}) == 1
        assert await _customer_count(resources, org.id, request.identity_value) == 1

    async def test_simultaneous_link_same_identity_same_customer_one_row(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        customer, _ = await resources.customers.resolve_or_create(org.id, _create_request())
        link = LinkIdentityRequest(
            identity_type=IdentityType.PHONE, identity_value="+506 1111 2222"
        )

        async def _link() -> Any:
            try:
                return await resources.customers.link_identity(org.id, customer.id, link)
            except Exception as exc:
                return exc

        results = await asyncio.gather(*[_link() for _ in range(5)])
        linked = [r for r in results if hasattr(r, "id")]
        assert len(linked) >= 1
        assert len({r.id for r in linked}) == 1
        identities = await resources.customers.list_identities(org.id, customer.id)
        assert len(identities) == 2

    async def test_simultaneous_link_same_identity_different_customers_one_winner(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        customer_a, _ = await resources.customers.resolve_or_create(org.id, _create_request())
        customer_b, _ = await resources.customers.resolve_or_create(org.id, _create_request())
        contested = LinkIdentityRequest(
            identity_type=IdentityType.EMAIL,
            identity_value=f"contested-{uuid.uuid4().hex[:8]}@example.com",
        )

        async def _link(customer_id: Any) -> Any:
            try:
                return await resources.customers.link_identity(org.id, customer_id, contested)
            except Exception as exc:
                return exc

        results = await asyncio.gather(_link(customer_a.id), _link(customer_b.id))
        winners = [r for r in results if hasattr(r, "id")]
        conflicts = [r for r in results if isinstance(r, IdentityConflictError)]
        assert len(winners) == 1
        assert len(conflicts) == 1

    async def test_simultaneous_open_same_external_thread_one_conversation(
        self, auth_client: Any, make_auth_org: Any
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

        async def _open() -> Any:
            try:
                return await resources.conversations.open_or_resolve(org.id, payload)
            except Exception as exc:
                return exc

        results = await asyncio.gather(*[_open() for _ in range(5)])
        conversations = [r[0] for r in results if isinstance(r, tuple)]
        assert len({c.id for c in conversations}) == 1
        async with resources.database.tenant_transaction(org.id) as tenant:
            count = (
                await tenant.session.execute(text("SELECT count(*) FROM conversations"))
            ).scalar_one()
        assert count == 1
