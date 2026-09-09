"""Messaging persistence (NXS-P09).

Every repository is tenant-scoped: constructed with a ``TenantSession`` so every query is
RLS-confined whatever the WHERE clause says, and the composite tenant-aware FKs refuse a
cross-tenant attachment at the database. The idempotency and inbound-receipt stores use
the NXS-P06 race-recovery pattern (a lost unique-constraint race re-resolves the
committed winner from a fresh transaction).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.exc import IntegrityError

from nexus_ai.domain.messaging.models import (
    MessagingAccountRecord,
    MessagingInboundReceiptRecord,
    MessagingMessageRecord,
    MessagingSecretRecord,
    MessagingSendIdempotencyRecord,
)
from nexus_ai.infrastructure.database import Database
from nexus_ai.infrastructure.tenant_session import TenantSession
from nexus_ai.integrations.credentials import CredentialType, EncryptedSecret
from nexus_ai.messaging.entities import (
    AccountStatus,
    EmailEnvelopeFields,
    Message,
    MessageAddress,
    MessageChannel,
    MessageContent,
    MessageDirection,
    MessageStatus,
    MessagingAccount,
    SmsSegmentInfo,
)
from nexus_ai.messaging.errors import MessagingAccountConflictError
from nexus_ai.messaging.idempotency import (
    SendClaimOutcome,
    SendIdempotencyRecord,
    SendIdempotencyStatus,
)


def _to_account(row: MessagingAccountRecord) -> MessagingAccount:
    return MessagingAccount(
        id=row.id,
        organization_id=row.organization_id,
        channel=MessageChannel(row.channel),
        provider=row.provider,
        slug=row.slug,
        external_account_id=row.external_account_id,
        sender_identity=row.sender_identity,
        credential_ref=row.credential_ref,
        status=AccountStatus(row.status),
        configuration=dict(row.configuration),
        webhook_token=row.webhook_token,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_message(row: MessagingMessageRecord) -> Message:
    return Message(
        id=row.id,
        organization_id=row.organization_id,
        conversation_id=row.conversation_id,
        customer_id=row.customer_id,
        channel=MessageChannel(row.channel),
        direction=MessageDirection(row.direction),
        status=MessageStatus(row.status),
        provider=row.provider,
        provider_account_id=row.account_id,
        provider_message_id=row.provider_message_id,
        sender=MessageAddress.model_validate(row.sender),
        recipients=tuple(MessageAddress.model_validate(item) for item in row.recipients),
        content=MessageContent.model_validate(row.content),
        email=None if row.email is None else EmailEnvelopeFields.model_validate(row.email),
        sms=None if row.sms is None else SmsSegmentInfo.model_validate(row.sms),
        reply_to_message_id=row.reply_to_message_id,
        correlation_id=row.correlation_id,
        idempotency_key=row.idempotency_key,
        error_code=row.error_code,
        provider_timestamp=row.provider_timestamp,
        sent_at=row.sent_at,
        delivered_at=row.delivered_at,
        read_at=row.read_at,
        failed_at=row.failed_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class MessagingAccountRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self._tenant = tenant
        self._session = tenant.session

    async def by_id(self, account_id: uuid.UUID) -> MessagingAccount | None:
        row = await self._session.get(MessagingAccountRecord, account_id)
        return None if row is None else _to_account(row)

    async def by_webhook_token(self, token: str) -> MessagingAccount | None:
        row = (
            (
                await self._session.execute(
                    select(MessagingAccountRecord).where(
                        MessagingAccountRecord.organization_id == self._tenant.organization_id,
                        MessagingAccountRecord.webhook_token == token,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )
        return None if row is None else _to_account(row)

    async def list_all(self, *, limit: int) -> list[MessagingAccount]:
        rows = (
            await self._session.execute(
                select(MessagingAccountRecord)
                .where(MessagingAccountRecord.organization_id == self._tenant.organization_id)
                .order_by(MessagingAccountRecord.created_at.desc(), MessagingAccountRecord.id)
                .limit(limit)
            )
        ).scalars()
        return [_to_account(row) for row in rows]

    async def insert(
        self,
        *,
        account_id: uuid.UUID,
        channel: MessageChannel,
        provider: str,
        slug: str,
        external_account_id: str,
        sender_identity: str,
        configuration: dict[str, Any],
        webhook_token: str,
    ) -> MessagingAccount:
        now = dt.datetime.now(dt.UTC)
        record = MessagingAccountRecord(
            id=account_id,
            organization_id=self._tenant.organization_id,
            channel=channel.value,
            provider=provider,
            slug=slug,
            external_account_id=external_account_id,
            sender_identity=sender_identity,
            credential_ref=None,
            status=AccountStatus.ACTIVE.value,
            configuration=configuration,
            webhook_token=webhook_token,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise MessagingAccountConflictError(
                "a messaging account with that slug or provider identity already exists",
                cause=exc,
            ) from exc
        await self._session.refresh(record)
        return _to_account(record)

    async def apply(
        self, account_id: uuid.UUID, *, changes: dict[str, Any]
    ) -> MessagingAccount | None:
        result = await self._session.execute(
            update(MessagingAccountRecord)
            .where(MessagingAccountRecord.id == account_id)
            .values(**changes, updated_at=dt.datetime.now(dt.UTC))
            .returning(MessagingAccountRecord)
        )
        row = result.scalars().one_or_none()
        return None if row is None else _to_account(row)

    async def delete_one(self, account_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            delete(MessagingAccountRecord).where(MessagingAccountRecord.id == account_id)
        )
        return bool(cast("CursorResult[Any]", result).rowcount)


class MessagingMessageRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self._tenant = tenant
        self._session = tenant.session

    async def by_id(self, message_id: uuid.UUID) -> Message | None:
        row = await self._session.get(MessagingMessageRecord, message_id)
        return None if row is None else _to_message(row)

    async def by_provider_id(
        self, account_id: uuid.UUID, direction: MessageDirection, provider_message_id: str
    ) -> Message | None:
        row = (
            (
                await self._session.execute(
                    select(MessagingMessageRecord).where(
                        MessagingMessageRecord.organization_id == self._tenant.organization_id,
                        MessagingMessageRecord.account_id == account_id,
                        MessagingMessageRecord.direction == direction.value,
                        MessagingMessageRecord.provider_message_id == provider_message_id,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )
        return None if row is None else _to_message(row)

    async def list_for_conversation(
        self, conversation_id: uuid.UUID, *, after_id: uuid.UUID | None, limit: int
    ) -> list[Message]:
        query = (
            select(MessagingMessageRecord)
            .where(
                MessagingMessageRecord.organization_id == self._tenant.organization_id,
                MessagingMessageRecord.conversation_id == conversation_id,
            )
            .order_by(MessagingMessageRecord.created_at.desc(), MessagingMessageRecord.id.desc())
            .limit(limit + 1)
        )
        if after_id is not None:
            query = query.where(MessagingMessageRecord.id < after_id)
        rows = (await self._session.execute(query)).scalars()
        return [_to_message(row) for row in rows]

    async def insert(self, message: Message) -> Message:
        record = MessagingMessageRecord(
            id=message.id,
            organization_id=self._tenant.organization_id,
            conversation_id=message.conversation_id,
            customer_id=message.customer_id,
            account_id=message.provider_account_id,
            channel=message.channel.value,
            direction=message.direction.value,
            status=message.status.value,
            provider=message.provider,
            provider_message_id=message.provider_message_id,
            sender=message.sender.model_dump(mode="json"),
            recipients=[address.model_dump(mode="json") for address in message.recipients],
            content=message.content.model_dump(mode="json"),
            email=None if message.email is None else message.email.model_dump(mode="json"),
            sms=None if message.sms is None else message.sms.model_dump(mode="json"),
            reply_to_message_id=message.reply_to_message_id,
            correlation_id=message.correlation_id,
            idempotency_key=message.idempotency_key,
            error_code=message.error_code,
            provider_timestamp=message.provider_timestamp,
            sent_at=message.sent_at,
            delivered_at=message.delivered_at,
            read_at=message.read_at,
            failed_at=message.failed_at,
            created_at=message.created_at,
            updated_at=message.updated_at,
        )
        self._session.add(record)
        await self._session.flush()
        await self._session.refresh(record)
        return _to_message(record)

    async def apply(self, message_id: uuid.UUID, changes: dict[str, Any]) -> Message | None:
        result = await self._session.execute(
            update(MessagingMessageRecord)
            .where(MessagingMessageRecord.id == message_id)
            .values(**changes, updated_at=dt.datetime.now(dt.UTC))
            .returning(MessagingMessageRecord)
        )
        row = result.scalars().one_or_none()
        return None if row is None else _to_message(row)


class MessagingInboundReceiptRepository:
    """Durable per-event dedupe / replay claim. ``claim`` returns True when THIS caller
    won the unique constraint (must process the event); False means already seen."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def claim(
        self, *, organization_id: uuid.UUID, account_id: uuid.UUID, event_id: str
    ) -> bool:
        try:
            async with self._db.tenant_transaction(organization_id) as tenant:
                tenant.session.add(
                    MessagingInboundReceiptRecord(
                        id=uuid.uuid7(),
                        organization_id=organization_id,
                        account_id=account_id,
                        event_id=event_id[:400],
                        message_id=None,
                        status="CLAIMED",
                        created_at=dt.datetime.now(dt.UTC),
                    )
                )
                await tenant.session.flush()
                return True
        except IntegrityError:
            return False

    async def mark(
        self,
        *,
        organization_id: uuid.UUID,
        account_id: uuid.UUID,
        event_id: str,
        status: str,
        message_id: uuid.UUID | None,
    ) -> None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            await tenant.session.execute(
                update(MessagingInboundReceiptRecord)
                .where(
                    MessagingInboundReceiptRecord.organization_id == organization_id,
                    MessagingInboundReceiptRecord.account_id == account_id,
                    MessagingInboundReceiptRecord.event_id == event_id[:400],
                )
                .values(status=status, message_id=message_id)
            )


class MessagingSendIdempotencyRepository:
    """Satisfies :class:`~nexus_ai.messaging.idempotency.SendIdempotencyStore`."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def claim(
        self,
        *,
        organization_id: uuid.UUID,
        account_id: uuid.UUID,
        idempotency_key: str,
        request_fingerprint: str,
        expires_at: dt.datetime,
    ) -> SendClaimOutcome:
        now = dt.datetime.now(dt.UTC)
        try:
            async with self._db.tenant_transaction(organization_id) as tenant:
                tenant.session.add(
                    MessagingSendIdempotencyRecord(
                        id=uuid.uuid7(),
                        organization_id=organization_id,
                        account_id=account_id,
                        idempotency_key=idempotency_key,
                        request_fingerprint=request_fingerprint,
                        status=SendIdempotencyStatus.PENDING.value,
                        message_id=None,
                        result_json=None,
                        error_code=None,
                        created_at=now,
                        updated_at=now,
                        expires_at=expires_at,
                    )
                )
                await tenant.session.flush()
                return SendClaimOutcome(is_owner=True)
        except IntegrityError:
            pass
        existing = await self.get(
            organization_id=organization_id,
            account_id=account_id,
            idempotency_key=idempotency_key,
        )
        return SendClaimOutcome(is_owner=False, existing=existing)

    async def finalize(
        self,
        *,
        organization_id: uuid.UUID,
        account_id: uuid.UUID,
        idempotency_key: str,
        status: SendIdempotencyStatus,
        message_id: uuid.UUID | None,
        result_json: dict[str, Any] | None,
        error_code: str | None,
    ) -> None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            await tenant.session.execute(
                update(MessagingSendIdempotencyRecord)
                .where(
                    MessagingSendIdempotencyRecord.organization_id == organization_id,
                    MessagingSendIdempotencyRecord.account_id == account_id,
                    MessagingSendIdempotencyRecord.idempotency_key == idempotency_key,
                )
                .values(
                    status=status.value,
                    message_id=message_id,
                    result_json=result_json,
                    error_code=error_code,
                    updated_at=dt.datetime.now(dt.UTC),
                )
            )

    async def get(
        self,
        *,
        organization_id: uuid.UUID,
        account_id: uuid.UUID,
        idempotency_key: str,
    ) -> SendIdempotencyRecord | None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            row = (
                (
                    await tenant.session.execute(
                        select(MessagingSendIdempotencyRecord).where(
                            MessagingSendIdempotencyRecord.organization_id == organization_id,
                            MessagingSendIdempotencyRecord.account_id == account_id,
                            MessagingSendIdempotencyRecord.idempotency_key == idempotency_key,
                        )
                    )
                )
                .scalars()
                .one_or_none()
            )
            if row is None:
                return None
            return SendIdempotencyRecord(
                idempotency_key=row.idempotency_key,
                request_fingerprint=row.request_fingerprint,
                status=SendIdempotencyStatus(row.status),
                message_id=row.message_id,
                result_json=None if row.result_json is None else dict(row.result_json),
                error_code=row.error_code,
                updated_at=row.updated_at,
            )


class MessagingSecretStore:
    """Encrypted-at-rest ciphertext persistence for messaging provider credentials —
    satisfies :class:`~nexus_ai.integrations.credentials.EncryptedSecretStore`. Same
    encryption seam as the NXS-P07 vault; a dedicated table isolates the two planes."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def get(self, organization_id: uuid.UUID, ref: str) -> EncryptedSecret | None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            row = (
                (
                    await tenant.session.execute(
                        select(MessagingSecretRecord).where(
                            MessagingSecretRecord.organization_id == organization_id,
                            MessagingSecretRecord.credential_ref == ref,
                        )
                    )
                )
                .scalars()
                .one_or_none()
            )
            if row is None:
                return None
            return EncryptedSecret(CredentialType(row.credential_type), row.ciphertext)

    async def put(self, organization_id: uuid.UUID, ref: str, secret: EncryptedSecret) -> None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            now = dt.datetime.now(dt.UTC)
            existing = (
                (
                    await tenant.session.execute(
                        select(MessagingSecretRecord).where(
                            MessagingSecretRecord.organization_id == organization_id,
                            MessagingSecretRecord.credential_ref == ref,
                        )
                    )
                )
                .scalars()
                .one_or_none()
            )
            if existing is not None:
                existing.credential_type = secret.credential_type.value
                existing.ciphertext = secret.ciphertext
                existing.updated_at = now
            else:
                tenant.session.add(
                    MessagingSecretRecord(
                        id=uuid.uuid7(),
                        organization_id=organization_id,
                        credential_ref=ref,
                        credential_type=secret.credential_type.value,
                        ciphertext=secret.ciphertext,
                        created_at=now,
                        updated_at=now,
                    )
                )
            await tenant.session.flush()

    async def delete(self, organization_id: uuid.UUID, ref: str) -> bool:
        async with self._db.tenant_transaction(organization_id) as tenant:
            result = await tenant.session.execute(
                delete(MessagingSecretRecord).where(
                    MessagingSecretRecord.organization_id == organization_id,
                    MessagingSecretRecord.credential_ref == ref,
                )
            )
            return cast("CursorResult[Any]", result).rowcount > 0
