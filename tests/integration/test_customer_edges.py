"""Customer/conversation edge-path coverage (NXS-CUSTOMER-001).

Service and repository branches the main matrices don't reach: verification-state
missing identity, invalid reopen transitions, PENDING close, participant lookup,
cursor paths and external-key conflicts.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from nexus_ai.core.errors import (
    ConversationNotFoundError,
    ConversationStateConflictError,
    CustomerNotFoundError,
    NotFoundError,
)
from nexus_ai.domain.customers.entities import (
    CreateConversationRequest,
    CreateCustomerRequest,
    IdentityType,
    IdentityVerificationState,
    LinkIdentityRequest,
    ParticipantType,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


def _create_request(**overrides: object) -> CreateCustomerRequest:
    payload: dict[str, object] = {
        "display_name": f"Edge {uuid.uuid4().hex[:8]}",
        "identity_type": IdentityType.EMAIL,
        "identity_value": f"edge-{uuid.uuid4().hex[:8]}@example.com",
        "identity_source": "test",
    }
    payload.update(overrides)
    return CreateCustomerRequest(**payload)


def _resources(client: Any) -> Any:
    return client.nexus_app.state.lifespan.resources  # type: ignore[attr-defined]


class TestMissingEntities:
    async def test_link_identity_requires_an_existing_customer(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        with pytest.raises(CustomerNotFoundError):
            await resources.customers.link_identity(
                org.id,
                uuid.uuid7(),
                LinkIdentityRequest(
                    identity_type=IdentityType.EMAIL, identity_value="ghost@example.com"
                ),
            )

    async def test_timeline_requires_an_existing_customer(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        with pytest.raises(CustomerNotFoundError):
            await resources.customers.timeline(org.id, uuid.uuid7(), after=None, limit=10)

    async def test_close_unknown_conversation_is_not_found(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        with pytest.raises(ConversationNotFoundError):
            await resources.conversations.close(org.id, uuid.uuid7())

    async def test_reopen_unknown_conversation_is_not_found(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        with pytest.raises(ConversationNotFoundError):
            await resources.conversations.reopen(org.id, uuid.uuid7())


class TestExternalThreadCustomerSemantics:
    """The four external-thread customer-compatibility cases (audit section 6)."""

    def _thread(self, **overrides: Any) -> CreateConversationRequest:
        payload: dict[str, Any] = {
            "channel": "whatsapp",
            "provider_namespace": "wa",
            "external_thread_id": f"sem-{uuid.uuid4().hex[:8]}",
        }
        payload.update(overrides)
        return CreateConversationRequest(**payload)

    async def test_same_customer_is_compatible(self, auth_client: Any, make_auth_org: Any) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        customer, _ = await resources.customers.resolve_or_create(org.id, _create_request())
        req = self._thread(customer_id=customer.id)
        first, created = await resources.conversations.open_or_resolve(org.id, req)
        assert created is True
        second, again = await resources.conversations.open_or_resolve(org.id, req)
        assert again is False
        assert second.id == first.id

    async def test_both_anonymous_is_compatible(self, auth_client: Any, make_auth_org: Any) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        req = self._thread()  # no customer_id
        first, _ = await resources.conversations.open_or_resolve(org.id, req)
        second, again = await resources.conversations.open_or_resolve(org.id, req)
        assert again is False
        assert second.id == first.id

    async def test_existing_has_customer_incoming_none_is_compatible(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        customer, _ = await resources.customers.resolve_or_create(org.id, _create_request())
        req = self._thread(customer_id=customer.id)
        first, _ = await resources.conversations.open_or_resolve(org.id, req)
        # A caller that asserts no customer accepts the thread's existing resolution.
        resolved, again = await resources.conversations.open_or_resolve(
            org.id, req.model_copy(update={"customer_id": None})
        )
        assert again is False
        assert resolved.id == first.id
        assert resolved.customer_id == customer.id

    async def test_existing_anonymous_incoming_customer_fails_closed(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        from nexus_ai.core.errors import ConversationCustomerConflictError

        org = await make_auth_org()
        resources = _resources(auth_client)
        customer, _ = await resources.customers.resolve_or_create(org.id, _create_request())
        req = self._thread()  # anonymous thread first
        await resources.conversations.open_or_resolve(org.id, req)
        # Now a caller explicitly claims the anonymous thread for a customer: ambiguous,
        # so fail closed rather than silently attach.
        with pytest.raises(ConversationCustomerConflictError):
            await resources.conversations.open_or_resolve(
                org.id, req.model_copy(update={"customer_id": customer.id})
            )


class TestServiceEdges:
    async def test_verification_state_unknown_identity_not_found(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        with pytest.raises(NotFoundError):
            await resources.customers.set_verification_state(
                org.id, uuid.uuid7(), IdentityVerificationState.VERIFIED
            )

    async def test_reopen_from_open_is_invalid_transition(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        conversation, _ = await resources.conversations.open_or_resolve(
            org.id, CreateConversationRequest(channel="web")
        )
        # PENDING → OPEN is the legal activation; OPEN → OPEN is invalid.
        activated = await resources.conversations.reopen(org.id, conversation.id)
        assert activated.status.value == "OPEN"
        with pytest.raises(ConversationStateConflictError):
            await resources.conversations.reopen(org.id, conversation.id)

    async def test_pending_conversation_can_close(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        conversation, _ = await resources.conversations.open_or_resolve(
            org.id, CreateConversationRequest(channel="web")
        )
        assert conversation.status.value == "PENDING"
        closed = await resources.conversations.close(org.id, conversation.id)
        assert closed.status.value == "CLOSED"

    async def test_reopen_then_close_roundtrip(self, auth_client: Any, make_auth_org: Any) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        conversation, _ = await resources.conversations.open_or_resolve(
            org.id, CreateConversationRequest(channel="web")
        )
        await resources.conversations.close(org.id, conversation.id)
        reopened = await resources.conversations.reopen(org.id, conversation.id)
        assert reopened.status.value == "OPEN"
        assert reopened.closed_at is None
        closed_again = await resources.conversations.close(org.id, conversation.id)
        assert closed_again.status.value == "CLOSED"


class TestRepositoryEdges:
    async def test_participant_find_and_idempotent_join(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        customer, _ = await resources.customers.resolve_or_create(org.id, _create_request())
        conversation, _ = await resources.conversations.open_or_resolve(
            org.id, CreateConversationRequest(customer_id=customer.id, channel="web")
        )
        from nexus_ai.domain.customers.repository import ConversationParticipantRepository

        async with resources.database.tenant_transaction(org.id) as tenant:
            repo = ConversationParticipantRepository(tenant)
            found = await repo.find(
                conversation_id=conversation.id,
                participant_type=ParticipantType.CUSTOMER,
                participant_ref=f"customer:{customer.id}",
            )
        assert found is not None
        # Joining the same participant again inside a NEW transaction is caught by
        # the unique constraint (the service treats it as idempotent).
        async with resources.database.tenant_transaction(org.id) as tenant:
            repo = ConversationParticipantRepository(tenant)
            with pytest.raises(DBAPIError):
                await repo.insert(
                    conversation_id=conversation.id,
                    participant_type=ParticipantType.CUSTOMER,
                    participant_ref=f"customer:{customer.id}",
                )

    async def test_conversation_cursor_pagination(
        self, auth_client: Any, make_auth_org: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        customer, _ = await resources.customers.resolve_or_create(org.id, _create_request())
        opened = []
        for _ in range(3):
            conversation, _ = await resources.conversations.open_or_resolve(
                org.id, CreateConversationRequest(customer_id=customer.id, channel="web")
            )
            opened.append(conversation)
        first_page = await resources.conversations.list_for_customer(
            org.id, customer.id, after_id=None, limit=2
        )
        assert len(first_page) == 2
        second_page = await resources.conversations.list_for_customer(
            org.id, customer.id, after_id=first_page[-1].id, limit=2
        )
        assert len(second_page) >= 1
        all_ids = {c.id for c in first_page + second_page}
        assert len(all_ids) == 3

    async def test_external_key_conflict_after_manual_insert(
        self, auth_client: Any, make_auth_org: Any, tenant_database: Any
    ) -> None:
        org = await make_auth_org()
        resources = _resources(auth_client)
        conversation, _ = await resources.conversations.open_or_resolve(
            org.id,
            CreateConversationRequest(
                channel="email",
                provider_namespace="smtp",
                external_thread_id=f"dup-thread-{uuid.uuid4().hex[:8]}",
            ),
        )
        # A duplicate external key inserted at the DB level must conflict; the
        # repository maps that to the deterministic error only on its own insert path,
        # so we prove the constraint directly and the service-level replay path.
        async with tenant_database.tenant_transaction(org.id) as tenant:
            with pytest.raises(DBAPIError):
                await tenant.session.execute(
                    text(
                        "INSERT INTO conversations "
                        "(id, organization_id, customer_id, channel, status, version, "
                        "provider_namespace, external_thread_id) "
                        "VALUES (:id, :org, NULL, 'email', 'PENDING', 1, 'smtp', :thread)"
                    ),
                    {
                        "id": uuid.uuid7(),
                        "org": org.id,
                        "thread": conversation.external_thread_id,
                    },
                )
        replayed, created = await resources.conversations.open_or_resolve(
            org.id,
            CreateConversationRequest(
                channel="email",
                provider_namespace="smtp",
                external_thread_id=conversation.external_thread_id,
            ),
        )
        assert created is False
        assert replayed.id == conversation.id
