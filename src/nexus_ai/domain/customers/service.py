"""Customer identity and conversation services (NXS-CUSTOMER-001).

Duplicate-safe by construction, with database constraints as the final authority. The
race-recovery contract (independent-audit corrective) is:

* ``resolve_or_create`` — N identical concurrent callers ALL succeed and ALL converge on
  ONE Customer. The first caller wins the canonical-identity unique constraint; every
  loser's transaction (its provisional Customer, timeline rows and outbox intent) rolls
  back ENTIRELY, and the loser re-resolves the winner from a fresh transaction — never
  from an aborted one. Exactly one Customer, one identity, one ``customers.created``
  event intent, one ``CUSTOMER_CREATED`` and one initial ``IDENTITY_LINKED`` activity.
* ``link_identity`` — identical (customer, identity) concurrent callers ALL succeed and
  return the same identity row. Different customers competing for one canonical identity
  produce exactly one winner and deterministic ``IdentityConflictError`` for the losers;
  an identity is never stolen or reassigned.
* ``open_or_resolve`` — a deterministic external thread key: every equivalent concurrent
  caller succeeds and converges on ONE Conversation, one ``conversations.opened`` event
  intent and one ``CONVERSATION_OPENED`` activity. A caller that explicitly names a
  different Customer than the thread already resolves to gets a deterministic
  ``ConversationCustomerConflictError`` — never a silent reattachment.

Timeline appends and P04 outbox events commit in the SAME transaction as their business
mutation; a NATS outage can never corrupt domain state. P04 transport stays at-least-
once — these guarantees are about exactly one *domain mutation and event intent* per
unique business creation, not exactly-once delivery.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
from uuid import UUID

from nexus_ai.core.config import Settings
from nexus_ai.core.context import current_context
from nexus_ai.core.errors import (
    ConversationCustomerConflictError,
    ConversationExternalKeyConflictError,
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
    IdentityType,
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

_IDENTITY_OWNED_BY_ANOTHER = (
    "this identity already belongs to another Customer in this Organization."
)

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

    # -- create / resolve --------------------------------------------------------------

    async def resolve_or_create(
        self, organization_id: UUID, request: CreateCustomerRequest
    ) -> tuple[Customer, bool]:
        """Duplicate-safe customer creation. Returns ``(customer, created)``.

        Concurrent identical calls converge on one Customer (race-recovery corrective)."""
        normalized = normalize_identity_value(
            request.identity_type, request.identity_value, default_country=request.default_country
        )
        try:
            async with self._db.tenant_transaction(organization_id) as tenant:
                identities = CustomerIdentityRepository(tenant)
                existing = await identities.resolve(request.identity_type, normalized)
                if existing is not None:
                    return await self._load_owner(tenant, existing.customer_id), False

                customer_id = uuid.uuid7()
                customer = await CustomerRepository(tenant).insert(
                    customer_id=customer_id,
                    display_name=request.display_name,
                    preferred_locale=request.preferred_locale,
                )
                # A concurrent identical caller may already hold the canonical-identity
                # lock: this insert raises IdentityConflictError, propagates out of the
                # transaction and rolls back the whole provisional Customer with it.
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
        except IdentityConflictError:
            # The losing transaction rolled back in full — no orphan Customer. Converge
            # on the committed winner from a clean transaction.
            pass
        return await self._converge_on_identity_owner(
            organization_id, request.identity_type, normalized
        ), False

    async def _converge_on_identity_owner(
        self, organization_id: UUID, identity_type: IdentityType, normalized: str
    ) -> Customer:
        async with self._db.tenant_transaction(organization_id) as tenant:
            winner = await CustomerIdentityRepository(tenant).resolve(identity_type, normalized)
            if winner is None:  # pragma: no cover - a unique violation implies a committed winner
                raise IdentityConflictError(
                    "the canonical identity could not be re-resolved after a race"
                )
            return await self._load_owner(tenant, winner.customer_id)

    @staticmethod
    async def _load_owner(tenant: TenantSession, customer_id: UUID) -> Customer:
        customer = await CustomerRepository(tenant).by_id(customer_id)
        if customer is None:  # pragma: no cover - the tenant-aware FK makes this unreachable
            raise CustomerNotFoundError("the resolved identity's Customer is missing")
        return customer

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

    # -- linking ---------------------------------------------------------------------

    async def link_identity(
        self, organization_id: UUID, customer_id: UUID, request: LinkIdentityRequest
    ) -> CustomerIdentity:
        """Link an identity to an existing Customer.

        Idempotent for the same (customer, identity) — including concurrent identical
        callers, which all converge on the winning identity row. A different owner (found
        directly or by losing the insert race) fails deterministically with
        ``IdentityConflictError`` and never steals the identity (race-recovery corrective).
        """
        normalized = normalize_identity_value(
            request.identity_type, request.identity_value, default_country=request.default_country
        )
        try:
            async with self._db.tenant_transaction(organization_id) as tenant:
                await self._require_customer(tenant, customer_id)
                identities = CustomerIdentityRepository(tenant)
                existing = await identities.resolve(request.identity_type, normalized)
                if existing is not None:
                    if existing.customer_id == customer_id:
                        return existing  # idempotent re-link
                    raise IdentityConflictError(_IDENTITY_OWNED_BY_ANOTHER)

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
        except IdentityConflictError:
            # Rolled back cleanly. Re-resolve the committed owner from a fresh
            # transaction: our own Customer → converge idempotently; anyone else →
            # deterministic conflict (fail closed, no identity stealing).
            pass
        async with self._db.tenant_transaction(organization_id) as tenant:
            winner = await CustomerIdentityRepository(tenant).resolve(
                request.identity_type, normalized
            )
            if winner is not None and winner.customer_id == customer_id:
                return winner
            raise IdentityConflictError(_IDENTITY_OWNED_BY_ANOTHER)

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

    # -- timeline ------------------------------------------------------------------

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

    # -- helpers ------------------------------------------------------------------

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
        """Deterministic conversation resolution.

        A deterministic external thread key resolves to the existing Conversation;
        otherwise exactly one is created. Concurrent equivalent callers all succeed and
        converge on one Conversation (race-recovery corrective). A caller that names a
        different Customer than the thread already resolves to fails deterministically.
        """
        has_key = bool(request.provider_namespace and request.external_thread_id)
        try:
            async with self._db.tenant_transaction(organization_id) as tenant:
                conversations = ConversationRepository(tenant)
                if has_key:
                    existing = await conversations.by_external_key(
                        request.channel,
                        request.provider_namespace,  # type: ignore[arg-type]
                        request.external_thread_id,  # type: ignore[arg-type]
                    )
                    if existing is not None:
                        self._assert_thread_compatible(existing, request)
                        return existing, False

                conversation = await conversations.insert(
                    conversation_id=uuid.uuid7(),
                    customer_id=request.customer_id,
                    channel=request.channel,
                    provider_namespace=request.provider_namespace,
                    external_thread_id=request.external_thread_id,
                    subject=request.subject,
                )
                if request.customer_id is not None:
                    await ConversationParticipantRepository(tenant).insert(
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
                            None
                            if conversation.customer_id is None
                            else str(conversation.customer_id)
                        ),
                        "channel": conversation.channel,
                    },
                )
                return conversation, True
        except ConversationExternalKeyConflictError:
            if not has_key:  # pragma: no cover - no key means no unique constraint to lose
                raise
        # Lost the external-thread race: the losing transaction rolled back in full.
        # Re-read the committed winner from a clean transaction and check compatibility.
        async with self._db.tenant_transaction(organization_id) as tenant:
            winner = await ConversationRepository(tenant).by_external_key(
                request.channel,
                request.provider_namespace,  # type: ignore[arg-type]
                request.external_thread_id,  # type: ignore[arg-type]
            )
            if winner is None:  # pragma: no cover - the unique violation implies a winner
                raise ConversationExternalKeyConflictError(
                    "the Conversation could not be re-resolved after a race"
                )
            self._assert_thread_compatible(winner, request)
            return winner, False

    @staticmethod
    def _assert_thread_compatible(
        existing: Conversation, request: CreateConversationRequest
    ) -> None:
        """Fail closed when a caller explicitly names a Customer the thread does not
        already resolve to. A caller that names no Customer asserts nothing and accepts
        whatever the deterministic thread resolves to."""
        if request.customer_id is None:
            return
        if existing.customer_id == request.customer_id:
            return
        raise ConversationCustomerConflictError(
            "this external thread already resolves to a different Customer.",
            extensions={
                "conversation_id": str(existing.id),
                "existing_customer_id": (
                    None if existing.customer_id is None else str(existing.customer_id)
                ),
                "requested_customer_id": str(request.customer_id),
            },
        )

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
            conversations = await ConversationRepository(tenant).for_customer(
                customer_id, after_id=after_id, limit=limit
            )
        # The repository fetches limit+1 to detect a next page; the service returns
        # exactly the requested page.
        return conversations[:limit]

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
