"""Customer identity and conversation integration matrix (NXS-CUSTOMER-001).

Real PostgreSQL: resolve-or-create, linking, conversations, timeline, events and
migration-level invariants — through the runtime role with forced RLS.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.core.errors import (
    CustomerNotFoundError,
    IdentityConflictError,
)
from nexus_ai.domain.customers.entities import (
    CreateConversationRequest,
    CreateCustomerRequest,
    IdentityType,
    IdentityVerificationState,
    LinkIdentityRequest,
)
from nexus_ai.domain.customers.normalization import normalize_identity_value

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


def _create_request(**overrides: object) -> CreateCustomerRequest:
    payload: dict[str, object] = {
        "display_name": f"Customer {uuid.uuid4().hex[:8]}",
        "identity_type": IdentityType.EMAIL,
        "identity_value": f"cust-{uuid.uuid4().hex[:8]}@example.com",
        "identity_source": "test",
    }
    payload.update(overrides)
    return CreateCustomerRequest(**payload)


def _resources(client: Any) -> Any:
    return client.nexus_app.state.lifespan.resources  # type: ignore[attr-defined]


class TestCustomerLifecycle:
    async def test_resolve_or_create_returns_same_customer_on_retry(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        request = _create_request()
        first, created = await resources.customers.resolve_or_create(org.id, request)
        assert created is True
        second, created_again = await resources.customers.resolve_or_create(org.id, request)
        assert created_again is False
        assert second.id == first.id

    async def test_same_identity_different_representation_resolves_same_customer(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        first, _ = await resources.customers.resolve_or_create(
            org.id, _create_request(identity_value="Mixed.Case@Example.com")
        )
        second, created = await resources.customers.resolve_or_create(
            org.id, _create_request(identity_value="mixed.case@example.com")
        )
        assert created is False
        assert second.id == first.id

    async def test_same_identity_cannot_belong_to_two_customers(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        identity_value = f"dup-{uuid.uuid4().hex[:8]}@example.com"
        first, _ = await resources.customers.resolve_or_create(
            org.id, _create_request(identity_value=identity_value)
        )
        second_customer, _ = await resources.customers.resolve_or_create(org.id, _create_request())
        with pytest.raises(IdentityConflictError):
            await resources.customers.link_identity(
                org.id,
                second_customer.id,
                LinkIdentityRequest(
                    identity_type=IdentityType.EMAIL, identity_value=identity_value
                ),
            )
        # And a fresh resolve-or-create with the same identity returns the ORIGINAL.
        again, created = await resources.customers.resolve_or_create(
            org.id, _create_request(identity_value=identity_value)
        )
        assert created is False
        assert again.id == first.id

    async def test_same_identity_across_two_organizations_is_independent(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org_a = await make_auth_org()
        org_b = await make_auth_org()
        resources = _resources(auth_client)
        shared = f"shared-{uuid.uuid4().hex[:8]}@example.com"
        customer_a, _ = await resources.customers.resolve_or_create(
            org_a.id, _create_request(identity_value=shared)
        )
        customer_b, _ = await resources.customers.resolve_or_create(
            org_b.id, _create_request(identity_value=shared)
        )
        assert customer_a.id != customer_b.id
        assert customer_a.organization_id == org_a.id
        assert customer_b.organization_id == org_b.id

    async def test_identity_linking_is_idempotent(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        customer, _ = await resources.customers.resolve_or_create(org.id, _create_request())
        link = LinkIdentityRequest(
            identity_type=IdentityType.PHONE,
            identity_value="+506 9999 0000",
        )
        first = await resources.customers.link_identity(org.id, customer.id, link)
        second = await resources.customers.link_identity(org.id, customer.id, link)
        assert first.id == second.id
        identities = await resources.customers.list_identities(org.id, customer.id)
        assert len(identities) == 2

    async def test_verification_state_mutation(self, auth_client: Any, make_auth_org: Any) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        customer, _ = await resources.customers.resolve_or_create(org.id, _create_request())
        identities = await resources.customers.list_identities(org.id, customer.id)
        identity_id = identities[0].id
        await resources.customers.set_verification_state(
            org.id, identity_id, IdentityVerificationState.VERIFIED
        )
        identities = await resources.customers.list_identities(org.id, customer.id)
        assert identities[0].verification_state is IdentityVerificationState.VERIFIED

    async def test_customer_not_found_in_other_organization(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org_a = await make_auth_org()
        org_b = await make_auth_org()
        resources = _resources(auth_client)
        customer, _ = await resources.customers.resolve_or_create(org_a.id, _create_request())
        with pytest.raises(CustomerNotFoundError):
            await resources.customers.get(org_b.id, customer.id)


class TestConversationLifecycle:
    async def test_open_close_idempotent_close(self, auth_client: Any, make_auth_org: Any) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        customer, _ = await resources.customers.resolve_or_create(org.id, _create_request())
        conversation, created = await resources.conversations.open_or_resolve(
            org.id,
            CreateConversationRequest(customer_id=customer.id, channel="web"),
        )
        assert created is True
        closed = await resources.conversations.close(org.id, conversation.id)
        assert closed.status.value == "CLOSED"
        again = await resources.conversations.close(org.id, conversation.id)
        assert again.status.value == "CLOSED"  # idempotent
        assert again.closed_at == closed.closed_at

    async def test_external_thread_key_resolves_same_conversation(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        customer, _ = await resources.customers.resolve_or_create(org.id, _create_request())
        payload = CreateConversationRequest(
            customer_id=customer.id,
            channel="whatsapp",
            provider_namespace="wa",
            external_thread_id="thread-0001",
        )
        first, created = await resources.conversations.open_or_resolve(org.id, payload)
        assert created is True
        second, created_again = await resources.conversations.open_or_resolve(org.id, payload)
        assert created_again is False
        assert second.id == first.id

    async def test_timeline_has_deterministic_order_and_dedup(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        customer, _ = await resources.customers.resolve_or_create(org.id, _create_request())
        conversation, _ = await resources.conversations.open_or_resolve(
            org.id, CreateConversationRequest(customer_id=customer.id, channel="web")
        )
        await resources.conversations.close(org.id, conversation.id)
        timeline = await resources.customers.timeline(org.id, customer.id, after=None, limit=50)
        keys = [(a.occurred_at, a.id) for a in timeline]
        assert keys == sorted(keys)  # (occurred_at, id) ordering
        activity_types = {a.activity_type for a in timeline}
        assert "customer.created" in activity_types
        assert "identity.linked" in activity_types
        assert "conversation.opened" in activity_types
        assert "conversation.closed" in activity_types

    async def test_events_committed_with_business_mutations(
        self, auth_client: Any, make_auth_org: Any, tenant_database: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        _customer, _ = await resources.customers.resolve_or_create(org.id, _create_request())
        async with tenant_database.tenant_transaction(org.id) as tenant:
            rows = (
                await tenant.session.execute(
                    text(
                        "SELECT event_type FROM event_outbox "
                        "WHERE event_type LIKE 'customers.%' ORDER BY event_type"
                    )
                )
            ).all()
        assert ("customers.created",) in rows


class TestNormalizationPipeline:
    async def test_resolution_uses_normalized_values(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        raw = "  Customer+Tag@Example.COM "
        normalized = normalize_identity_value(IdentityType.EMAIL, raw)
        _customer, _ = await resources.customers.resolve_or_create(
            org.id, _create_request(identity_value=raw)
        )
        async with resources.database.tenant_transaction(org.id) as tenant:
            stored = (
                await tenant.session.execute(
                    text("SELECT normalized_value FROM customer_identities")
                )
            ).scalar_one()
        assert stored == normalized
