"""Telephony persistence (NXS-P11).

Every repository is tenant-scoped: constructed with a ``TenantSession`` so every query is
RLS-confined whatever the WHERE clause says, and the composite tenant-aware FKs refuse a
cross-tenant attachment at the database. The call-event store is the durable provider
idempotency claim; call state transitions take a row lock (``SELECT ... FOR UPDATE``).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select, update

from nexus_ai.domain.telephony.models import (
    TelephonyAccountRecord,
    TelephonyCallEventRecord,
    TelephonyCallRecord,
    TelephonyMediaSessionRecord,
    TelephonyPhoneNumberRecord,
    TelephonySecretRecord,
)
from nexus_ai.infrastructure.database import Database
from nexus_ai.infrastructure.tenant_session import TenantSession
from nexus_ai.integrations.credentials import CredentialType, EncryptedSecret
from nexus_ai.telephony.entities import (
    AccountStatus,
    Call,
    CallDirection,
    CallDisposition,
    CallLeg,
    CallState,
    MediaCodecInfo,
    MediaDirection,
    MediaSession,
    MediaState,
    PhoneNumber,
    TelephonyAccount,
    TelephonyProvider,
)


def _to_account(row: TelephonyAccountRecord) -> TelephonyAccount:
    return TelephonyAccount(
        id=row.id,
        organization_id=row.organization_id,
        provider=TelephonyProvider(row.provider),
        slug=row.slug,
        external_account_id=row.external_account_id,
        credential_ref=row.credential_ref,
        status=AccountStatus(row.status),
        configuration=dict(row.configuration),
        webhook_token=row.webhook_token,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_number(row: TelephonyPhoneNumberRecord) -> PhoneNumber:
    return PhoneNumber(
        id=row.id,
        organization_id=row.organization_id,
        account_id=row.account_id,
        e164=row.e164,
        display_name=row.display_name,
        verified=row.verified,
        inbound_enabled=row.inbound_enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_call(row: TelephonyCallRecord) -> Call:
    return Call(
        id=row.id,
        organization_id=row.organization_id,
        account_id=row.account_id,
        from_number_id=row.from_number_id,
        direction=CallDirection(row.direction),
        state=CallState(row.state),
        disposition=None if row.disposition is None else CallDisposition(row.disposition),
        state_rank=row.state_rank,
        provider=TelephonyProvider(row.provider),
        provider_call_id=row.provider_call_id,
        from_address=row.from_address,
        to_address=row.to_address,
        legs=tuple(CallLeg.model_validate(leg) for leg in row.legs),
        correlation_id=row.correlation_id,
        idempotency_key=row.idempotency_key,
        request_fingerprint=row.request_fingerprint,
        error_code=row.error_code,
        provider_timestamp=row.provider_timestamp,
        provider_sequence=row.provider_sequence,
        ringing_at=row.ringing_at,
        answered_at=row.answered_at,
        ended_at=row.ended_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_media(row: TelephonyMediaSessionRecord) -> MediaSession:
    return MediaSession(
        id=row.id,
        organization_id=row.organization_id,
        call_id=row.call_id,
        direction=MediaDirection(row.direction),
        state=MediaState(row.state),
        bridge_id=row.bridge_id,
        stream_id=row.stream_id,
        codec=None if row.codec is None else MediaCodecInfo.model_validate(row.codec),
        started_at=row.started_at,
        stopped_at=row.stopped_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class TelephonyAccountRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self._tenant = tenant
        self._session = tenant.session

    async def by_id(self, account_id: uuid.UUID) -> TelephonyAccount | None:
        row = await self._session.get(TelephonyAccountRecord, account_id)
        if row is None or row.organization_id != self._tenant.organization_id:
            return None
        return _to_account(row)

    async def by_webhook_token(self, token: str) -> TelephonyAccount | None:
        row = (
            (
                await self._session.execute(
                    select(TelephonyAccountRecord).where(
                        TelephonyAccountRecord.organization_id == self._tenant.organization_id,
                        TelephonyAccountRecord.webhook_token == token,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )
        return None if row is None else _to_account(row)

    async def list_all(self, *, limit: int) -> list[TelephonyAccount]:
        rows = (
            (
                await self._session.execute(
                    select(TelephonyAccountRecord)
                    .where(TelephonyAccountRecord.organization_id == self._tenant.organization_id)
                    .order_by(TelephonyAccountRecord.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_to_account(row) for row in rows]

    async def insert(
        self,
        *,
        provider: TelephonyProvider,
        slug: str,
        external_account_id: str,
        configuration: dict[str, Any],
        webhook_token: str,
    ) -> TelephonyAccount:
        now = dt.datetime.now(dt.UTC)
        record = TelephonyAccountRecord(
            id=uuid.uuid7(),
            organization_id=self._tenant.organization_id,
            provider=provider.value,
            slug=slug,
            external_account_id=external_account_id,
            credential_ref=None,
            status=AccountStatus.ACTIVE.value,
            configuration=configuration,
            webhook_token=webhook_token,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        await self._session.flush()
        await self._session.refresh(record)
        return _to_account(record)

    async def apply(
        self, account_id: uuid.UUID, changes: dict[str, Any]
    ) -> TelephonyAccount | None:
        row = (
            (
                await self._session.execute(
                    update(TelephonyAccountRecord)
                    .where(
                        TelephonyAccountRecord.organization_id == self._tenant.organization_id,
                        TelephonyAccountRecord.id == account_id,
                    )
                    .values(**changes, updated_at=dt.datetime.now(dt.UTC))
                    .returning(TelephonyAccountRecord)
                )
            )
            .scalars()
            .one_or_none()
        )
        return None if row is None else _to_account(row)

    async def delete_one(self, account_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            delete(TelephonyAccountRecord).where(
                TelephonyAccountRecord.organization_id == self._tenant.organization_id,
                TelephonyAccountRecord.id == account_id,
            )
        )
        return cast("CursorResult[Any]", result).rowcount > 0


class TelephonyPhoneNumberRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self._tenant = tenant
        self._session = tenant.session

    async def by_id(self, number_id: uuid.UUID) -> PhoneNumber | None:
        row = await self._session.get(TelephonyPhoneNumberRecord, number_id)
        if row is None or row.organization_id != self._tenant.organization_id:
            return None
        return _to_number(row)

    async def by_e164(self, e164: str) -> PhoneNumber | None:
        row = (
            (
                await self._session.execute(
                    select(TelephonyPhoneNumberRecord).where(
                        TelephonyPhoneNumberRecord.organization_id == self._tenant.organization_id,
                        TelephonyPhoneNumberRecord.e164 == e164,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )
        return None if row is None else _to_number(row)

    async def list_all(self, *, limit: int) -> list[PhoneNumber]:
        rows = (
            (
                await self._session.execute(
                    select(TelephonyPhoneNumberRecord)
                    .where(
                        TelephonyPhoneNumberRecord.organization_id == self._tenant.organization_id
                    )
                    .order_by(TelephonyPhoneNumberRecord.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_to_number(row) for row in rows]

    async def insert(
        self,
        *,
        account_id: uuid.UUID,
        e164: str,
        display_name: str | None,
        inbound_enabled: bool,
        verified: bool,
    ) -> PhoneNumber:
        now = dt.datetime.now(dt.UTC)
        record = TelephonyPhoneNumberRecord(
            id=uuid.uuid7(),
            organization_id=self._tenant.organization_id,
            account_id=account_id,
            e164=e164,
            display_name=display_name,
            verified=verified,
            inbound_enabled=inbound_enabled,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        await self._session.flush()
        await self._session.refresh(record)
        return _to_number(record)

    async def apply(self, number_id: uuid.UUID, changes: dict[str, Any]) -> PhoneNumber | None:
        row = (
            (
                await self._session.execute(
                    update(TelephonyPhoneNumberRecord)
                    .where(
                        TelephonyPhoneNumberRecord.organization_id == self._tenant.organization_id,
                        TelephonyPhoneNumberRecord.id == number_id,
                    )
                    .values(**changes, updated_at=dt.datetime.now(dt.UTC))
                    .returning(TelephonyPhoneNumberRecord)
                )
            )
            .scalars()
            .one_or_none()
        )
        return None if row is None else _to_number(row)


class TelephonyCallRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self._tenant = tenant
        self._session = tenant.session

    async def by_id(self, call_id: uuid.UUID, *, for_update: bool = False) -> Call | None:
        query = select(TelephonyCallRecord).where(
            TelephonyCallRecord.organization_id == self._tenant.organization_id,
            TelephonyCallRecord.id == call_id,
        )
        if for_update:
            query = query.with_for_update()
        row = (await self._session.execute(query)).scalars().one_or_none()
        return None if row is None else _to_call(row)

    async def by_provider_call_id(
        self, account_id: uuid.UUID, provider_call_id: str, *, for_update: bool = False
    ) -> Call | None:
        query = select(TelephonyCallRecord).where(
            TelephonyCallRecord.organization_id == self._tenant.organization_id,
            TelephonyCallRecord.account_id == account_id,
            TelephonyCallRecord.provider_call_id == provider_call_id,
        )
        if for_update:
            query = query.with_for_update()
        row = (await self._session.execute(query)).scalars().one_or_none()
        return None if row is None else _to_call(row)

    async def by_idempotency_key(self, idempotency_key: str) -> Call | None:
        row = (
            (
                await self._session.execute(
                    select(TelephonyCallRecord).where(
                        TelephonyCallRecord.organization_id == self._tenant.organization_id,
                        TelephonyCallRecord.idempotency_key == idempotency_key,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )
        return None if row is None else _to_call(row)

    async def list_all(self, *, account_id: uuid.UUID | None, limit: int) -> list[Call]:
        query = select(TelephonyCallRecord).where(
            TelephonyCallRecord.organization_id == self._tenant.organization_id
        )
        if account_id is not None:
            query = query.where(TelephonyCallRecord.account_id == account_id)
        rows = (
            (
                await self._session.execute(
                    query.order_by(TelephonyCallRecord.created_at.desc()).limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_to_call(row) for row in rows]

    async def insert(
        self,
        *,
        call_id: uuid.UUID,
        account_id: uuid.UUID,
        from_number_id: uuid.UUID | None,
        direction: CallDirection,
        state: CallState,
        state_rank: int,
        provider: TelephonyProvider,
        provider_call_id: str | None,
        from_address: str,
        to_address: str,
        legs: list[CallLeg],
        correlation_id: str | None,
        idempotency_key: str | None,
        provider_timestamp: dt.datetime | None,
        provider_sequence: int | None,
        request_fingerprint: str | None = None,
        ringing_at: dt.datetime | None = None,
        answered_at: dt.datetime | None = None,
    ) -> Call:
        now = dt.datetime.now(dt.UTC)
        record = TelephonyCallRecord(
            id=call_id,
            organization_id=self._tenant.organization_id,
            account_id=account_id,
            from_number_id=from_number_id,
            direction=direction.value,
            state=state.value,
            disposition=None,
            state_rank=state_rank,
            provider=provider.value,
            provider_call_id=provider_call_id,
            from_address=from_address,
            to_address=to_address,
            legs=[leg.model_dump(mode="json") for leg in legs],
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            error_code=None,
            provider_timestamp=provider_timestamp,
            provider_sequence=provider_sequence,
            ringing_at=ringing_at,
            answered_at=answered_at,
            ended_at=None,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        await self._session.flush()
        await self._session.refresh(record)
        return _to_call(record)

    async def apply(self, call_id: uuid.UUID, changes: dict[str, Any]) -> Call | None:
        row = (
            (
                await self._session.execute(
                    update(TelephonyCallRecord)
                    .where(
                        TelephonyCallRecord.organization_id == self._tenant.organization_id,
                        TelephonyCallRecord.id == call_id,
                    )
                    .values(**changes, updated_at=dt.datetime.now(dt.UTC))
                    .returning(TelephonyCallRecord)
                )
            )
            .scalars()
            .one_or_none()
        )
        return None if row is None else _to_call(row)


class TelephonyCallEventRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self._tenant = tenant
        self._session = tenant.session

    async def by_provider_event(
        self, account_id: uuid.UUID, provider_event_id: str
    ) -> TelephonyCallEventRecord | None:
        return (
            (
                await self._session.execute(
                    select(TelephonyCallEventRecord).where(
                        TelephonyCallEventRecord.organization_id == self._tenant.organization_id,
                        TelephonyCallEventRecord.account_id == account_id,
                        TelephonyCallEventRecord.provider_event_id == provider_event_id,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )

    async def record(
        self,
        *,
        account_id: uuid.UUID,
        call_id: uuid.UUID | None,
        provider_event_id: str,
        provider_call_id: str,
        event_type: str,
        outcome: str,
        applied_state: str | None,
        detail: dict[str, Any],
    ) -> None:
        self._session.add(
            TelephonyCallEventRecord(
                id=uuid.uuid7(),
                organization_id=self._tenant.organization_id,
                account_id=account_id,
                call_id=call_id,
                provider_event_id=provider_event_id,
                provider_call_id=provider_call_id,
                event_type=event_type,
                outcome=outcome,
                applied_state=applied_state,
                detail=detail,
                created_at=dt.datetime.now(dt.UTC),
            )
        )
        await self._session.flush()

    async def count_for_call(self, call_id: uuid.UUID) -> int:
        return int(
            (
                await self._session.execute(
                    select(func.count())
                    .select_from(TelephonyCallEventRecord)
                    .where(
                        TelephonyCallEventRecord.organization_id == self._tenant.organization_id,
                        TelephonyCallEventRecord.call_id == call_id,
                    )
                )
            ).scalar_one()
        )


class TelephonyMediaSessionRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self._tenant = tenant
        self._session = tenant.session

    async def by_call(self, call_id: uuid.UUID) -> list[MediaSession]:
        rows = (
            (
                await self._session.execute(
                    select(TelephonyMediaSessionRecord).where(
                        TelephonyMediaSessionRecord.organization_id == self._tenant.organization_id,
                        TelephonyMediaSessionRecord.call_id == call_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        return [_to_media(row) for row in rows]

    async def by_bridge(self, call_id: uuid.UUID, bridge_id: str) -> MediaSession | None:
        row = (
            (
                await self._session.execute(
                    select(TelephonyMediaSessionRecord).where(
                        TelephonyMediaSessionRecord.organization_id == self._tenant.organization_id,
                        TelephonyMediaSessionRecord.call_id == call_id,
                        TelephonyMediaSessionRecord.bridge_id == bridge_id,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )
        return None if row is None else _to_media(row)

    async def insert(
        self,
        *,
        call_id: uuid.UUID,
        direction: MediaDirection,
        state: MediaState,
        bridge_id: str | None,
        stream_id: str | None,
        started_at: dt.datetime | None,
    ) -> MediaSession:
        now = dt.datetime.now(dt.UTC)
        record = TelephonyMediaSessionRecord(
            id=uuid.uuid7(),
            organization_id=self._tenant.organization_id,
            call_id=call_id,
            direction=direction.value,
            state=state.value,
            bridge_id=bridge_id,
            stream_id=stream_id,
            codec=None,
            started_at=started_at,
            stopped_at=None,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        await self._session.flush()
        await self._session.refresh(record)
        return _to_media(record)

    async def apply(self, media_id: uuid.UUID, changes: dict[str, Any]) -> MediaSession | None:
        row = (
            (
                await self._session.execute(
                    update(TelephonyMediaSessionRecord)
                    .where(
                        TelephonyMediaSessionRecord.organization_id == self._tenant.organization_id,
                        TelephonyMediaSessionRecord.id == media_id,
                    )
                    .values(**changes, updated_at=dt.datetime.now(dt.UTC))
                    .returning(TelephonyMediaSessionRecord)
                )
            )
            .scalars()
            .one_or_none()
        )
        return None if row is None else _to_media(row)


class TelephonySecretStore:
    """Encrypted-at-rest ciphertext persistence for telephony provider credentials
    (ARI user/password, provider API key, webhook secret). Satisfies
    :class:`~nexus_ai.integrations.credentials.EncryptedSecretStore`."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def get(self, organization_id: uuid.UUID, ref: str) -> EncryptedSecret | None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            row = (
                (
                    await tenant.session.execute(
                        select(TelephonySecretRecord).where(
                            TelephonySecretRecord.organization_id == organization_id,
                            TelephonySecretRecord.credential_ref == ref,
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
                        select(TelephonySecretRecord).where(
                            TelephonySecretRecord.organization_id == organization_id,
                            TelephonySecretRecord.credential_ref == ref,
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
                    TelephonySecretRecord(
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
                delete(TelephonySecretRecord).where(
                    TelephonySecretRecord.organization_id == organization_id,
                    TelephonySecretRecord.credential_ref == ref,
                )
            )
            return cast("CursorResult[Any]", result).rowcount > 0
