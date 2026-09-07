"""Customer identity and conversation services (NXS-CUSTOMER-001).

Duplicate-safe by construction, with database constraints as the final authority:

* ``resolve_or_create`` — the canonical identity resolves to the existing Customer
  (unique constraint) or creates Customer + identity + timeline activity + outbox
  event in ONE tenant transaction. Concurrent races collapse to one Customer via the
  identity unique constraint and a post-conflict re-resolution.
* ``link_identity`` — idempotent when the identity is already this Customer's;
  deterministic ``IdentityConflictError`` when it belongs to another Customer (no
  silent identity stealing).
* ``open_conversation`` — a deterministic external thread key resolves to the
  existing Conversation or creates exactly one (unique constraint).
* Timeline appends and P04 outbox events commit in the SAME transaction as their
  business mutation; a NATS outage can never corrupt domain state.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
from uuid import UUID

from nexus_ai.core.config import Settings
from nexus_ai.core.context import current_context
from nexus_ai.core.errors import (
    ConversationNotFoundError,
    ConversationStateConflictError,
    CustomerNotFoundError,
    IdentityConflictError,
)
from nexus_ai.core.logging import get_logger
from nexus_ai.domain.customers.entities import (
    ActivityType,
    Conversation,
    ConversationStatus,
    CreateConversationRequest,
    CreateCustomerRequest,
    Customer,
    CustomerIdentity,
    IdentityVerificationState,
    LinkIdentityRequest,
    ParticipantType,
    TimelineActivityView,
)
from nexus_ai.domain.customers.normalization import normalize_identity_value
from nexus_ai.domain.customers.repository import (
    ConversationActivityRepository,
    ConversationParticipantRepository,
    ConversationRepository,
    CustomerIdentityRepository,
    CustomerRepository,
)
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.publisher import EventPublisher
from nexus_ai.infrastructure.database import Database
from nexus_ai.infrastructure.tenant_session import TenantSession

_ALLOWED_CONVERSATION_TRANSITIONS: dict[ConversationStatus, tuple[ConversationStatus, ...]] = {
    ConversationStatus.PENDING: (ConversationStatus.OPEN, ConversationStatus.CLOSED),
    ConversationStatus.OPEN: (ConversationStatus.CLOSED,),
    # Reopening is explicitly justified: later channel phases resume a deterministic
    # external thread after a pause (e.g. a customer replies to an old email thread).
    ConversationStatus.CLOSED: (ConversationStatus.OPEN,),
}


class CustomerService:
    def __init__(self, settings: Settings, database: Database, publisher: EventPublisher) -> None:
        self._settings = settings
        self._db = database
        self._publisher = publisher
        self._log = get_logger("nexus_ai.customers")

    # -- create / resolve ----------------------------------------------------------

    async def resolve_or_create(
        self, organization_id: UUID, request: CreateCustomerRequest
    ) -> tuple[Customer, bool]:
        """Duplicate-safe customer creation. Returns (customer, created)."""
        normalized = normalize_identity_value(
            request.identity_type, request.identity_value, default_country=request.default_country
        )
        async with self._db.tenant_transaction(organization_id) as tenant:
            identities = CustomerIdentityRepository(tenant)
            existing = await identities.resolve(request.identity_type, normalized)
            if existing is not None:
                customer = await CustomerRepository(tenant).by_id(existing.customer_id)
                if customer is None:  # pragma: no cover - FK makes this unreachable
                    raise CustomerNotFoundError("the resolved identity's Customer is missing")
                return customer, False
            customer_id = uuid.uuid7()
            customer = await CustomerRepository(tenant).insert(
                customer_id=customer_id,
                display_name=request.display_name,
                preferred_locale=request.preferred_locale,
            )
            identity = await identities.insert(
                identity_id=uuid.uuid7(),
                customer_id=customer_id,
                identity_type=request.identity_type,
                normalized_value=normalized,
                source=request.identity_source,
                is_primary=True,
            )
            await ConversationActivityRepository(tenant).append(
                customer_id=customer_id,
                conversation_id=None,
                activity_type=ActivityType.CUSTOMER_CREATED,
                dedup_key=f"customer.created:{customer_id}",
                data={"display_name": customer.display_name},
            )
            await ConversationActivityRepository(tenant).append(
                customer_id=customer_id,
                conversation_id=None,
                activity_type=ActivityType.IDENTITY_LINKED,
                dedup_key=f"identity.linked:{identity.id}",
                data={
                    "identity_id": str(identity.id),
                    "identity_type": identity.identity_type.value,
                },
            )
            await self._enqueue(
                tenant.session,
                event_type="customers.created",
                organization_id=organization_id,
                aggregate_id=str(customer_id),
                payload={
                    "customer_id": str(customer_id),
                    "initial_identity_id": str(identity.id),
                    "identity_type": identity.identity_type.value,
                },
            )
            return customer, True

    async def get(self, organization_id: UUID, customer_id: UUID) -> Customer:
        async with self._db.tenant_transaction(organization_id) as tenant:
            customer = await CustomerRepository(tenant).by_id(customer_id)
            if customer is None:
                raise CustomerNotFoundError("the Customer does not exist in this Organization")
            return customer

    async def list_identities(
        self, organization_id: UUID, customer_id: UUID
    ) -> list[CustomerIdentity]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            await self._require_customer(tenant, customer_id)
            return await CustomerIdentityRepository(tenant).for_customer(customer_id)

    # -- linking --------------------------------------------------------------------

    async def link_identity(
        self, organization_id: UUID, customer_id: UUID, request: LinkIdentityRequest
    ) -> CustomerIdentity:
        """Link a new identity to an existing Customer. Idempotent for the same
        (customer, identity); a conflicting owner fails deterministically."""
        normalized = normalize_identity_value(
            request.identity_type, request.identity_value, default_country=request.default_country
        )
        async with self._db.tenant_transaction(organization_id) as tenant:
            await self._require_customer(tenant, customer_id)
            identities = CustomerIdentityRepository(tenant)
            existing = await identities.resolve(request.identity_type, normalized)
            if existing is not None:
                if existing.customer_id == customer_id:
                    return existing  # idempotent re-link
                raise IdentityConflictError(
                    "this identity already belongs to another Customer in this Organization."
                )
            identity = await identities.insert(
                identity_id=uuid.uuid7(),
                customer_id=customer_id,
                identity_type=request.identity_type,
                normalized_value=normalized,
                source=request.identity_source,
            )
            await ConversationActivityRepository(tenant).append(
                customer_id=customer_id,
                conversation_id=None,
                activity_type=ActivityType.IDENTITY_LINKED,
                dedup_key=f"identity.linked:{identity.id}",
                data={
                    "identity_id": str(identity.id),
                    "identity_type": identity.identity_type.value,
                },
            )
            await self._enqueue(
                tenant.session,
                event_type="customers.identity.linked",
                organization_id=organization_id,
                aggregate_id=str(customer_id),
                payload={
                    "customer_id": str(customer_id),
                    "identity_id": str(identity.id),
                    "identity_type": identity.identity_type.value,
                },
            )
            return identity

    async def set_verification_state(
        self,
        organization_id: UUID,
        identity_id: UUID,
        state: IdentityVerificationState,
    ) -> None:
        """The controlled verification-mutation seam (P10 owns actual verification)."""
        async with self._db.tenant_transaction(organization_id) as tenant:
            identities = CustomerIdentityRepository(tenant)
            identity = await identities.by_id(identity_id)
            if identity is None:
                from nexus_ai.core.errors import NotFoundError

                raise NotFoundError("the identity does not exist in this Organization")
            await identities.set_verification_state(identity_id, state)
            await self._enqueue(
                tenant.session,
                event_type="customers.identity.status_changed",
                organization_id=organization_id,
                aggregate_id=str(identity_id),
                payload={
                    "customer_id": str(identity.customer_id),
                    "identity_id": str(identity_id),
                    "verification_state": state.value,
                },
            )

    # -- timeline -------------------------------------------------------------------

    async def timeline(
        self,
        organization_id: UUID,
        customer_id: UUID,
        *,
        after: tuple[dt.datetime, UUID] | None,
        limit: int,
    ) -> list[TimelineActivityView]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            await self._require_customer(tenant, customer_id)
            return await ConversationActivityRepository(tenant).timeline(
                customer_id, after=after, limit=limit
            )

    # -- helpers --------------------------------------------------------------------

    async def _require_customer(self, tenant: TenantSession, customer_id: UUID) -> Customer:
        customer = await CustomerRepository(tenant).by_id(customer_id)
        if customer is None:
            raise CustomerNotFoundError("the Customer does not exist in this Organization")
        return customer

    async def _enqueue(
        self,
        session: Any,
        *,
        event_type: str,
        organization_id: UUID,
        aggregate_id: str,
        payload: dict[str, Any],
    ) -> None:
        request_context = current_context()
        envelope = EventEnvelope.create(
            event_type=event_type,
            event_version=1,
            aggregate_type="customer",
            aggregate_id=aggregate_id,
            producer=self._settings.service_name,
            organization_id=organization_id,
            correlation_id=(None if request_context is None else request_context.correlation_id),
            payload=payload,
        )
        await self._publisher.enqueue(session, envelope)


class ConversationService:
    def __init__(self, settings: Settings, database: Database, publisher: EventPublisher) -> None:
        self._settings = settings
        self._db = database
        self._publisher = publisher
        self._log = get_logger("nexus_ai.conversations")

    async def open_or_resolve(
        self, organization_id: UUID, request: CreateConversationRequest
    ) -> tuple[Conversation, bool]:
        """Deterministic conversation resolution: an external thread key resolves to
        the existing Conversation; otherwise exactly one is created."""
        async with self._db.tenant_transaction(organization_id) as tenant:
            conversations = ConversationRepository(tenant)
            if request.provider_namespace and request.external_thread_id:
                existing = await conversations.by_external_key(
                    request.channel, request.provider_namespace, request.external_thread_id
                )
                if existing is not None:
                    return existing, False
            conversation = await conversations.insert(
                conversation_id=uuid.uuid7(),
                customer_id=request.customer_id,
                channel=request.channel,
                provider_namespace=request.provider_namespace,
                external_thread_id=request.external_thread_id,
                subject=request.subject,
            )
            participants = ConversationParticipantRepository(tenant)
            if request.customer_id is not None:
                await participants.insert(
                    conversation_id=conversation.id,
                    participant_type=ParticipantType.CUSTOMER,
                    participant_ref=f"customer:{request.customer_id}",
                )
            await ConversationActivityRepository(tenant).append(
                customer_id=request.customer_id,
                conversation_id=conversation.id,
                activity_type=ActivityType.CONVERSATION_OPENED,
                dedup_key=f"conversation.opened:{conversation.id}",
                data={"channel": conversation.channel},
            )
            await self._enqueue(
                tenant.session,
                event_type="conversations.opened",
                organization_id=organization_id,
                aggregate_id=str(conversation.id),
                payload={
                    "conversation_id": str(conversation.id),
                    "customer_id": (
                        None if conversation.customer_id is None else str(conversation.customer_id)
                    ),
                    "channel": conversation.channel,
                },
            )
            return conversation, True

    async def get(self, organization_id: UUID, conversation_id: UUID) -> Conversation:
        async with self._db.tenant_transaction(organization_id) as tenant:
            conversation = await ConversationRepository(tenant).by_id(conversation_id)
            if conversation is None:
                raise ConversationNotFoundError(
                    "the Conversation does not exist in this Organization"
                )
            return conversation

    async def list_for_customer(
        self,
        organization_id: UUID,
        customer_id: UUID,
        *,
        after_id: UUID | None,
        limit: int,
    ) -> list[Conversation]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await ConversationRepository(tenant).for_customer(
                customer_id, after_id=after_id, limit=limit
            )

    async def close(self, organization_id: UUID, conversation_id: UUID) -> Conversation:
        """Idempotent close: closing an already-closed conversation is a no-op."""
        async with self._db.tenant_transaction(organization_id) as tenant:
            conversations = ConversationRepository(tenant)
            conversation = await conversations.by_id(conversation_id)
            if conversation is None:
                raise ConversationNotFoundError(
                    "the Conversation does not exist in this Organization"
                )
            if conversation.status is ConversationStatus.CLOSED:
                return conversation
            return await self._transition(
                tenant, conversation, ConversationStatus.CLOSED, event_type="conversations.closed"
            )

    async def reopen(self, organization_id: UUID, conversation_id: UUID) -> Conversation:
        """Explicit CLOSED → OPEN (justified seam for later channel phases)."""
        async with self._db.tenant_transaction(organization_id) as tenant:
            conversations = ConversationRepository(tenant)
            conversation = await conversations.by_id(conversation_id)
            if conversation is None:
                raise ConversationNotFoundError(
                    "the Conversation does not exist in this Organization"
                )
            return await self._transition(
                tenant, conversation, ConversationStatus.OPEN, event_type="conversations.opened"
            )

    async def _transition(
        self,
        tenant: TenantSession,
        conversation: Conversation,
        target: ConversationStatus,
        *,
        event_type: str,
    ) -> Conversation:
        allowed = _ALLOWED_CONVERSATION_TRANSITIONS.get(conversation.status, ())
        if target not in allowed:
            raise ConversationStateConflictError(
                f"Conversation state {conversation.status.value} cannot transition to "
                f"{target.value}.",
                extensions={
                    "current_status": conversation.status.value,
                    "requested_status": target.value,
                },
            )
        conversations = ConversationRepository(tenant)
        updated = await conversations.transition(conversation, target)
        if updated is None:  # pragma: no cover - RLS makes this unreachable
            raise ConversationNotFoundError("the Conversation is not visible in this scope")
        await ConversationActivityRepository(tenant).append(
            customer_id=updated.customer_id,
            conversation_id=updated.id,
            activity_type=(
                ActivityType.CONVERSATION_CLOSED
                if target is ConversationStatus.CLOSED
                else ActivityType.CONVERSATION_OPENED
            ),
            dedup_key=f"{event_type}:{updated.id}:{updated.version}",
            data={"channel": updated.channel},
        )
        await self._enqueue(
            tenant.session,
            event_type=event_type,
            organization_id=tenant.organization_id,
            aggregate_id=str(updated.id),
            payload={
                "conversation_id": str(updated.id),
                "customer_id": (None if updated.customer_id is None else str(updated.customer_id)),
                "channel": updated.channel,
            },
        )
        return updated

    async def _enqueue(
        self,
        session: Any,
        *,
        event_type: str,
        organization_id: UUID,
        aggregate_id: str,
        payload: dict[str, Any],
    ) -> None:
        request_context = current_context()
        envelope = EventEnvelope.create(
            event_type=event_type,
            event_version=1,
            aggregate_type="conversation",
            aggregate_id=aggregate_id,
            producer=self._settings.service_name,
            organization_id=organization_id,
            correlation_id=(None if request_context is None else request_context.correlation_id),
            payload=payload,
        )
        await self._publisher.enqueue(session, envelope)
