"""Customer and conversation persistence (NXS-CUSTOMER-001).

Every repository is tenant-scoped: constructed with a ``TenantSession``, so every
query is RLS-confined to the bound Organization whatever the WHERE clause says, and
the composite tenant-aware FKs refuse cross-tenant attachments at the database.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import literal, select, tuple_, update
from sqlalchemy.exc import IntegrityError

from nexus_ai.core.errors import (
    ConversationExternalKeyConflictError,
    IdentityConflictError,
)
from nexus_ai.domain.customers.entities import (
    ActivityType,
    Conversation,
    ConversationParticipant,
    ConversationStatus,
    Customer,
    CustomerIdentity,
    CustomerStatus,
    IdentityStatus,
    IdentityType,
    IdentityVerificationState,
    ParticipantType,
    TimelineActivityView,
)
from nexus_ai.domain.customers.models import (
    ConversationActivityRecord,
    ConversationParticipantRecord,
    ConversationRecord,
    CustomerIdentityRecord,
    CustomerRecord,
)
from nexus_ai.infrastructure.tenant_session import TenantSession


def _to_customer(row: CustomerRecord) -> Customer:
    return Customer(
        id=row.id,
        organization_id=row.organization_id,
        display_name=row.display_name,
        status=IdentityStatus(row.status),
        preferred_locale=row.preferred_locale,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_identity(row: CustomerIdentityRecord) -> CustomerIdentity:
    return CustomerIdentity(
        id=row.id,
        organization_id=row.organization_id,
        customer_id=row.customer_id,
        identity_type=IdentityType(row.identity_type),
        normalized_value=row.normalized_value,
        verification_state=IdentityVerificationState(row.verification_state),
        is_primary=row.is_primary,
        source=row.source,
        status=CustomerStatus(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_conversation(row: ConversationRecord) -> Conversation:
    return Conversation(
        id=row.id,
        organization_id=row.organization_id,
        customer_id=row.customer_id,
        channel=row.channel,
        status=ConversationStatus(row.status),
        provider_namespace=row.provider_namespace,
        external_thread_id=row.external_thread_id,
        subject=row.subject,
        version=row.version,
        opened_at=row.opened_at,
        last_activity_at=row.last_activity_at,
        closed_at=row.closed_at,
    )


class CustomerRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self._tenant = tenant
        self._session = tenant.session

    async def by_id(self, customer_id: uuid.UUID) -> Customer | None:
        row = await self._session.get(CustomerRecord, customer_id)
        return None if row is None else _to_customer(row)

    async def insert(
        self, *, customer_id: uuid.UUID, display_name: str, preferred_locale: str | None
    ) -> Customer:
        now = dt.datetime.now(dt.UTC)
        record = CustomerRecord(
            id=customer_id,
            organization_id=self._tenant.organization_id,
            display_name=display_name,
            status=IdentityStatus.ACTIVE.value,
            preferred_locale=preferred_locale,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        await self._session.flush()
        await self._session.refresh(record)
        return _to_customer(record)


class CustomerIdentityRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self._tenant = tenant
        self._session = tenant.session

    async def resolve(
        self, identity_type: IdentityType, normalized_value: str
    ) -> CustomerIdentity | None:
        row = (
            (
                await self._session.execute(
                    select(CustomerIdentityRecord).where(
                        CustomerIdentityRecord.organization_id == self._tenant.organization_id,
                        CustomerIdentityRecord.identity_type == identity_type.value,
                        CustomerIdentityRecord.normalized_value == normalized_value,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )
        return None if row is None else _to_identity(row)

    async def by_id(self, identity_id: uuid.UUID) -> CustomerIdentity | None:
        row = await self._session.get(CustomerIdentityRecord, identity_id)
        return None if row is None else _to_identity(row)

    async def for_customer(self, customer_id: uuid.UUID) -> list[CustomerIdentity]:
        rows = (
            await self._session.execute(
                select(CustomerIdentityRecord)
                .where(CustomerIdentityRecord.customer_id == customer_id)
                .order_by(CustomerIdentityRecord.created_at)
            )
        ).scalars()
        return [_to_identity(row) for row in rows]

    async def insert(
        self,
        *,
        identity_id: uuid.UUID,
        customer_id: uuid.UUID,
        identity_type: IdentityType,
        normalized_value: str,
        source: str,
        is_primary: bool = False,
    ) -> CustomerIdentity:
        now = dt.datetime.now(dt.UTC)
        record = CustomerIdentityRecord(
            id=identity_id,
            organization_id=self._tenant.organization_id,
            customer_id=customer_id,
            identity_type=identity_type.value,
            normalized_value=normalized_value,
            verification_state=IdentityVerificationState.UNVERIFIED.value,
            is_primary=is_primary,
            source=source,
            status=CustomerStatus.ACTIVE.value,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if "uq_customer_identities_canonical" not in str(exc.orig):
                raise
            raise IdentityConflictError(
                "this identity already belongs to another Customer in this Organization.",
                cause=exc,
            ) from exc
        await self._session.refresh(record)
        return _to_identity(record)

    async def set_verification_state(
        self, identity_id: uuid.UUID, state: IdentityVerificationState
    ) -> None:
        await self._session.execute(
            update(CustomerIdentityRecord)
            .where(CustomerIdentityRecord.id == identity_id)
            .values(
                verification_state=state.value,
                status=(
                    IdentityStatus.REVOKED.value
                    if state is IdentityVerificationState.REVOKED
                    else IdentityStatus.ACTIVE.value
                ),
                updated_at=dt.datetime.now(dt.UTC),
            )
        )


class ConversationRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self._tenant = tenant
        self._session = tenant.session

    async def by_id(self, conversation_id: uuid.UUID) -> Conversation | None:
        row = await self._session.get(ConversationRecord, conversation_id)
        return None if row is None else _to_conversation(row)

    async def by_external_key(
        self, channel: str, provider_namespace: str, external_thread_id: str
    ) -> Conversation | None:
        row = (
            (
                await self._session.execute(
                    select(ConversationRecord).where(
                        ConversationRecord.organization_id == self._tenant.organization_id,
                        ConversationRecord.channel == channel,
                        ConversationRecord.provider_namespace == provider_namespace,
                        ConversationRecord.external_thread_id == external_thread_id,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )
        return None if row is None else _to_conversation(row)

    async def for_customer(
        self, customer_id: uuid.UUID, *, after_id: uuid.UUID | None, limit: int
    ) -> list[Conversation]:
        query = (
            select(ConversationRecord)
            .where(ConversationRecord.customer_id == customer_id)
            .order_by(ConversationRecord.opened_at.desc(), ConversationRecord.id.desc())
            .limit(limit + 1)
        )
        if after_id is not None:
            query = query.where(ConversationRecord.id < after_id)
        rows = (await self._session.execute(query)).scalars()
        return [_to_conversation(row) for row in rows]

    async def insert(
        self,
        *,
        conversation_id: uuid.UUID,
        customer_id: uuid.UUID | None,
        channel: str,
        provider_namespace: str | None,
        external_thread_id: str | None,
        subject: str | None,
    ) -> Conversation:
        now = dt.datetime.now(dt.UTC)
        record = ConversationRecord(
            id=conversation_id,
            organization_id=self._tenant.organization_id,
            customer_id=customer_id,
            channel=channel,
            status=ConversationStatus.PENDING.value,
            provider_namespace=provider_namespace,
            external_thread_id=external_thread_id,
            subject=subject,
            version=1,
            opened_at=now,
            last_activity_at=now,
        )
        self._session.add(record)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if "uq_conversations_external_thread" not in str(exc.orig):
                raise
            raise ConversationExternalKeyConflictError(
                "a Conversation with that external thread key already exists.",
                cause=exc,
            ) from exc
        await self._session.refresh(record)
        return _to_conversation(record)

    async def transition(
        self, conversation: Conversation, target: ConversationStatus
    ) -> Conversation | None:
        changes: dict[str, Any] = {
            "status": target.value,
            "version": conversation.version + 1,
            "last_activity_at": dt.datetime.now(dt.UTC),
        }
        if target is ConversationStatus.CLOSED:
            changes["closed_at"] = dt.datetime.now(dt.UTC)
        elif target is ConversationStatus.OPEN:
            changes["closed_at"] = None
        result = await self._session.execute(
            update(ConversationRecord)
            .where(ConversationRecord.id == conversation.id)
            .values(**changes)
            .returning(ConversationRecord)
        )
        updated = result.scalars().one_or_none()
        return None if updated is None else _to_conversation(updated)


class ConversationParticipantRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self._tenant = tenant
        self._session = tenant.session

    async def insert(
        self,
        *,
        conversation_id: uuid.UUID,
        participant_type: ParticipantType,
        participant_ref: str,
    ) -> ConversationParticipant:
        record = ConversationParticipantRecord(
            id=uuid.uuid7(),
            organization_id=self._tenant.organization_id,
            conversation_id=conversation_id,
            participant_type=participant_type.value,
            participant_ref=participant_ref,
            joined_at=dt.datetime.now(dt.UTC),
        )
        self._session.add(record)
        try:
            await self._session.flush()
        except IntegrityError:
            # Idempotent join: the unique participant identity already exists — the
            # transaction is aborted, so the service re-reads in a fresh transaction.
            raise
        await self._session.refresh(record)
        return ConversationParticipant(
            id=record.id,
            organization_id=record.organization_id,
            conversation_id=record.conversation_id,
            participant_type=ParticipantType(record.participant_type),
            participant_ref=record.participant_ref,
            joined_at=record.joined_at,
            left_at=record.left_at,
        )

    async def find(
        self,
        *,
        conversation_id: uuid.UUID,
        participant_type: ParticipantType,
        participant_ref: str,
    ) -> ConversationParticipant | None:
        row = (
            (
                await self._session.execute(
                    select(ConversationParticipantRecord).where(
                        ConversationParticipantRecord.conversation_id == conversation_id,
                        ConversationParticipantRecord.participant_type == participant_type.value,
                        ConversationParticipantRecord.participant_ref == participant_ref,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )
        if row is None:
            return None
        return ConversationParticipant(
            id=row.id,
            organization_id=row.organization_id,
            conversation_id=row.conversation_id,
            participant_type=ParticipantType(row.participant_type),
            participant_ref=row.participant_ref,
            joined_at=row.joined_at,
            left_at=row.left_at,
        )


class ConversationActivityRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self._tenant = tenant
        self._session = tenant.session

    async def append(
        self,
        *,
        customer_id: uuid.UUID | None,
        conversation_id: uuid.UUID | None,
        activity_type: ActivityType,
        dedup_key: str | None,
        data: dict[str, object],
        occurred_at: dt.datetime | None = None,
    ) -> TimelineActivityView | None:
        record = ConversationActivityRecord(
            id=uuid.uuid7(),
            organization_id=self._tenant.organization_id,
            customer_id=customer_id,
            conversation_id=conversation_id,
            activity_type=activity_type.value,
            dedup_key=dedup_key,
            occurred_at=occurred_at or dt.datetime.now(dt.UTC),
            data=data,
        )
        self._session.add(record)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if "uq_conversation_activities_dedup" in str(exc.orig):
                # Replay-safe: the same dedup key appends exactly once. The caller's
                # business effect stays idempotent; None signals "already recorded"
                # and the service treats it as success without inventing data.
                return None
            raise
        await self._session.refresh(record)
        return TimelineActivityView(
            id=record.id,
            organization_id=record.organization_id,
            customer_id=record.customer_id,
            conversation_id=record.conversation_id,
            activity_type=record.activity_type,
            occurred_at=record.occurred_at,
            data=dict(record.data),
        )

    async def timeline(
        self,
        customer_id: uuid.UUID,
        *,
        after: tuple[dt.datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[TimelineActivityView]:
        query = (
            select(ConversationActivityRecord)
            .where(ConversationActivityRecord.customer_id == customer_id)
            .order_by(
                ConversationActivityRecord.occurred_at,
                ConversationActivityRecord.id,
            )
            .limit(limit + 1)
        )
        if after is not None:
            occurred_at, activity_id = after
            query = query.where(
                tuple_(ConversationActivityRecord.occurred_at, ConversationActivityRecord.id)
                > tuple_(literal(occurred_at), literal(activity_id))
            )
        rows = (await self._session.execute(query)).scalars()
        return [
            TimelineActivityView(
                id=row.id,
                organization_id=row.organization_id,
                customer_id=row.customer_id,
                conversation_id=row.conversation_id,
                activity_type=row.activity_type,
                occurred_at=row.occurred_at,
                data=dict(row.data),
            )
            for row in rows
        ]
