"""Voice persistence (NXS-P12).

Every repository is tenant-scoped: constructed with a ``TenantSession`` so every query is
RLS-confined whatever the WHERE clause says, and the composite tenant-aware FKs into the
NXS-P11 telephony tables refuse a cross-tenant attachment at the database. The
provider-event store is the durable idempotency claim; session transitions take a row
lock (``SELECT ... FOR UPDATE``).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select, update

from nexus_ai.domain.voice.models import (
    VoiceProfileRecord,
    VoiceProviderAccountRecord,
    VoiceProviderEventRecord,
    VoiceSecretRecord,
    VoiceSessionRecord,
    VoiceUsageRecordRecord,
)
from nexus_ai.infrastructure.database import Database
from nexus_ai.infrastructure.tenant_session import TenantSession
from nexus_ai.integrations.credentials import CredentialType, EncryptedSecret
from nexus_ai.voice.audio import AudioFormat
from nexus_ai.voice.entities import (
    VoiceAccountStatus,
    VoiceHandoffState,
    VoiceLatencyMetrics,
    VoiceProfile,
    VoiceProfileStatus,
    VoiceProvider,
    VoiceProviderAccount,
    VoiceSession,
    VoiceSessionDirection,
    VoiceUsage,
)
from nexus_ai.voice.state_machine import VoiceSessionDisposition, VoiceSessionState


def _fmt(data: Any) -> AudioFormat:
    return AudioFormat.model_validate(data)


def _to_account(row: VoiceProviderAccountRecord) -> VoiceProviderAccount:
    return VoiceProviderAccount(
        id=row.id,
        organization_id=row.organization_id,
        provider=VoiceProvider(row.provider),
        slug=row.slug,
        external_account_id=row.external_account_id,
        credential_ref=row.credential_ref,
        status=VoiceAccountStatus(row.status),
        configuration=dict(row.configuration),
        webhook_token=row.webhook_token,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_profile(row: VoiceProfileRecord) -> VoiceProfile:
    return VoiceProfile(
        id=row.id,
        organization_id=row.organization_id,
        account_id=row.account_id,
        slug=row.slug,
        display_name=row.display_name,
        provider_voice_ref=row.provider_voice_ref,
        provider_model_ref=row.provider_model_ref,
        status=VoiceProfileStatus(row.status),
        input_format=_fmt(row.input_format),
        output_format=_fmt(row.output_format),
        config=dict(row.config),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_session(row: VoiceSessionRecord) -> VoiceSession:
    return VoiceSession(
        id=row.id,
        organization_id=row.organization_id,
        account_id=row.account_id,
        voice_profile_id=row.voice_profile_id,
        call_id=row.call_id,
        media_session_id=row.media_session_id,
        direction=VoiceSessionDirection(row.direction),
        state=VoiceSessionState(row.state),
        disposition=None if row.disposition is None else VoiceSessionDisposition(row.disposition),
        state_rank=row.state_rank,
        handoff_state=VoiceHandoffState(row.handoff_state),
        provider=VoiceProvider(row.provider),
        provider_session_id=row.provider_session_id,
        bridge_ref=row.bridge_ref,
        negotiated_format=None if row.negotiated_format is None else _fmt(row.negotiated_format),
        idempotency_key=row.idempotency_key,
        request_fingerprint=row.request_fingerprint,
        error_code=row.error_code,
        latency=None if row.latency is None else VoiceLatencyMetrics.model_validate(row.latency),
        usage=None if row.usage is None else VoiceUsage.model_validate(row.usage),
        correlation_id=row.correlation_id,
        provider_timestamp=row.provider_timestamp,
        provider_sequence=row.provider_sequence,
        connecting_at=row.connecting_at,
        connected_at=row.connected_at,
        ended_at=row.ended_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class VoiceProviderAccountRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self._tenant = tenant
        self._session = tenant.session

    async def by_id(self, account_id: uuid.UUID) -> VoiceProviderAccount | None:
        row = await self._session.get(VoiceProviderAccountRecord, account_id)
        if row is None or row.organization_id != self._tenant.organization_id:
            return None
        return _to_account(row)

    async def by_webhook_token(self, token: str) -> VoiceProviderAccount | None:
        row = (
            (
                await self._session.execute(
                    select(VoiceProviderAccountRecord).where(
                        VoiceProviderAccountRecord.organization_id == self._tenant.organization_id,
                        VoiceProviderAccountRecord.webhook_token == token,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )
        return None if row is None else _to_account(row)

    async def list_all(self, *, limit: int) -> list[VoiceProviderAccount]:
        rows = (
            (
                await self._session.execute(
                    select(VoiceProviderAccountRecord)
                    .where(
                        VoiceProviderAccountRecord.organization_id == self._tenant.organization_id
                    )
                    .order_by(VoiceProviderAccountRecord.created_at.desc())
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
        provider: VoiceProvider,
        slug: str,
        external_account_id: str,
        configuration: dict[str, Any],
        webhook_token: str,
    ) -> VoiceProviderAccount:
        now = dt.datetime.now(dt.UTC)
        record = VoiceProviderAccountRecord(
            id=uuid.uuid7(),
            organization_id=self._tenant.organization_id,
            provider=provider.value,
            slug=slug,
            external_account_id=external_account_id,
            credential_ref=None,
            status=VoiceAccountStatus.ACTIVE.value,
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
    ) -> VoiceProviderAccount | None:
        row = (
            (
                await self._session.execute(
                    update(VoiceProviderAccountRecord)
                    .where(
                        VoiceProviderAccountRecord.organization_id == self._tenant.organization_id,
                        VoiceProviderAccountRecord.id == account_id,
                    )
                    .values(**changes, updated_at=dt.datetime.now(dt.UTC))
                    .returning(VoiceProviderAccountRecord)
                )
            )
            .scalars()
            .one_or_none()
        )
        return None if row is None else _to_account(row)

    async def delete_one(self, account_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            delete(VoiceProviderAccountRecord).where(
                VoiceProviderAccountRecord.organization_id == self._tenant.organization_id,
                VoiceProviderAccountRecord.id == account_id,
            )
        )
        return cast("CursorResult[Any]", result).rowcount > 0


class VoiceProfileRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self._tenant = tenant
        self._session = tenant.session

    async def by_id(self, profile_id: uuid.UUID) -> VoiceProfile | None:
        row = await self._session.get(VoiceProfileRecord, profile_id)
        if row is None or row.organization_id != self._tenant.organization_id:
            return None
        return _to_profile(row)

    async def list_all(self, *, limit: int) -> list[VoiceProfile]:
        rows = (
            (
                await self._session.execute(
                    select(VoiceProfileRecord)
                    .where(VoiceProfileRecord.organization_id == self._tenant.organization_id)
                    .order_by(VoiceProfileRecord.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_to_profile(row) for row in rows]

    async def insert(
        self,
        *,
        account_id: uuid.UUID,
        slug: str,
        display_name: str,
        provider_voice_ref: str,
        provider_model_ref: str | None,
        input_format: AudioFormat,
        output_format: AudioFormat,
        config: dict[str, Any],
    ) -> VoiceProfile:
        now = dt.datetime.now(dt.UTC)
        record = VoiceProfileRecord(
            id=uuid.uuid7(),
            organization_id=self._tenant.organization_id,
            account_id=account_id,
            slug=slug,
            display_name=display_name,
            provider_voice_ref=provider_voice_ref,
            provider_model_ref=provider_model_ref,
            status=VoiceProfileStatus.ACTIVE.value,
            input_format=input_format.model_dump(mode="json"),
            output_format=output_format.model_dump(mode="json"),
            config=config,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        await self._session.flush()
        await self._session.refresh(record)
        return _to_profile(record)

    async def apply(self, profile_id: uuid.UUID, changes: dict[str, Any]) -> VoiceProfile | None:
        row = (
            (
                await self._session.execute(
                    update(VoiceProfileRecord)
                    .where(
                        VoiceProfileRecord.organization_id == self._tenant.organization_id,
                        VoiceProfileRecord.id == profile_id,
                    )
                    .values(**changes, updated_at=dt.datetime.now(dt.UTC))
                    .returning(VoiceProfileRecord)
                )
            )
            .scalars()
            .one_or_none()
        )
        return None if row is None else _to_profile(row)


class VoiceSessionRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self._tenant = tenant
        self._session = tenant.session

    async def by_id(
        self, session_id: uuid.UUID, *, for_update: bool = False
    ) -> VoiceSession | None:
        query = select(VoiceSessionRecord).where(
            VoiceSessionRecord.organization_id == self._tenant.organization_id,
            VoiceSessionRecord.id == session_id,
        )
        if for_update:
            query = query.with_for_update()
        row = (await self._session.execute(query)).scalars().one_or_none()
        return None if row is None else _to_session(row)

    async def by_idempotency_key(self, idempotency_key: str) -> VoiceSession | None:
        row = (
            (
                await self._session.execute(
                    select(VoiceSessionRecord).where(
                        VoiceSessionRecord.organization_id == self._tenant.organization_id,
                        VoiceSessionRecord.idempotency_key == idempotency_key,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )
        return None if row is None else _to_session(row)

    async def live_for_media_session(
        self, media_session_id: uuid.UUID, *, for_update: bool = False
    ) -> VoiceSession | None:
        live = ("PENDING", "CONNECTING", "CONNECTED", "STREAMING", "ENDING")
        query = select(VoiceSessionRecord).where(
            VoiceSessionRecord.organization_id == self._tenant.organization_id,
            VoiceSessionRecord.media_session_id == media_session_id,
            VoiceSessionRecord.state.in_(live),
        )
        if for_update:
            query = query.with_for_update()
        row = (await self._session.execute(query)).scalars().one_or_none()
        return None if row is None else _to_session(row)

    async def by_provider_session_id(
        self, account_id: uuid.UUID, provider_session_id: str, *, for_update: bool = False
    ) -> VoiceSession | None:
        query = select(VoiceSessionRecord).where(
            VoiceSessionRecord.organization_id == self._tenant.organization_id,
            VoiceSessionRecord.account_id == account_id,
            VoiceSessionRecord.provider_session_id == provider_session_id,
        )
        if for_update:
            query = query.with_for_update()
        row = (await self._session.execute(query)).scalars().one_or_none()
        return None if row is None else _to_session(row)

    async def list_all(
        self, *, account_id: uuid.UUID | None, call_id: uuid.UUID | None, limit: int
    ) -> list[VoiceSession]:
        query = select(VoiceSessionRecord).where(
            VoiceSessionRecord.organization_id == self._tenant.organization_id
        )
        if account_id is not None:
            query = query.where(VoiceSessionRecord.account_id == account_id)
        if call_id is not None:
            query = query.where(VoiceSessionRecord.call_id == call_id)
        rows = (
            (
                await self._session.execute(
                    query.order_by(VoiceSessionRecord.created_at.desc()).limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_to_session(row) for row in rows]

    async def insert(
        self,
        *,
        session_id: uuid.UUID,
        account_id: uuid.UUID,
        voice_profile_id: uuid.UUID | None,
        call_id: uuid.UUID,
        media_session_id: uuid.UUID,
        direction: VoiceSessionDirection,
        state: VoiceSessionState,
        state_rank: int,
        provider: VoiceProvider,
        idempotency_key: str | None,
        request_fingerprint: str | None,
        correlation_id: str | None,
    ) -> VoiceSession:
        now = dt.datetime.now(dt.UTC)
        record = VoiceSessionRecord(
            id=session_id,
            organization_id=self._tenant.organization_id,
            account_id=account_id,
            voice_profile_id=voice_profile_id,
            call_id=call_id,
            media_session_id=media_session_id,
            direction=direction.value,
            state=state.value,
            disposition=None,
            state_rank=state_rank,
            handoff_state=VoiceHandoffState.AI.value,
            provider=provider.value,
            provider_session_id=None,
            bridge_ref=None,
            negotiated_format=None,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            error_code=None,
            latency=None,
            usage=None,
            correlation_id=correlation_id,
            provider_timestamp=None,
            provider_sequence=None,
            connecting_at=None,
            connected_at=None,
            ended_at=None,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        await self._session.flush()
        await self._session.refresh(record)
        return _to_session(record)

    async def apply(self, session_id: uuid.UUID, changes: dict[str, Any]) -> VoiceSession | None:
        row = (
            (
                await self._session.execute(
                    update(VoiceSessionRecord)
                    .where(
                        VoiceSessionRecord.organization_id == self._tenant.organization_id,
                        VoiceSessionRecord.id == session_id,
                    )
                    .values(**changes, updated_at=dt.datetime.now(dt.UTC))
                    .returning(VoiceSessionRecord)
                )
            )
            .scalars()
            .one_or_none()
        )
        return None if row is None else _to_session(row)


class VoiceProviderEventRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self._tenant = tenant
        self._session = tenant.session

    async def by_provider_event(
        self, account_id: uuid.UUID, provider_event_id: str
    ) -> VoiceProviderEventRecord | None:
        return (
            (
                await self._session.execute(
                    select(VoiceProviderEventRecord).where(
                        VoiceProviderEventRecord.organization_id == self._tenant.organization_id,
                        VoiceProviderEventRecord.account_id == account_id,
                        VoiceProviderEventRecord.provider_event_id == provider_event_id,
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
        session_id: uuid.UUID | None,
        provider_event_id: str,
        kind: str,
        outcome: str,
        detail: dict[str, Any],
    ) -> None:
        self._session.add(
            VoiceProviderEventRecord(
                id=uuid.uuid7(),
                organization_id=self._tenant.organization_id,
                account_id=account_id,
                session_id=session_id,
                provider_event_id=provider_event_id,
                kind=kind,
                outcome=outcome,
                detail=detail,
                created_at=dt.datetime.now(dt.UTC),
            )
        )
        await self._session.flush()


class VoiceUsageRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self._tenant = tenant
        self._session = tenant.session

    async def by_session(self, session_id: uuid.UUID) -> VoiceUsageRecordRecord | None:
        return (
            (
                await self._session.execute(
                    select(VoiceUsageRecordRecord).where(
                        VoiceUsageRecordRecord.organization_id == self._tenant.organization_id,
                        VoiceUsageRecordRecord.session_id == session_id,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )

    async def upsert(
        self,
        *,
        session_id: uuid.UUID,
        usage: VoiceUsage,
        latency: VoiceLatencyMetrics,
    ) -> None:
        existing = await self.by_session(session_id)
        now = dt.datetime.now(dt.UTC)
        if existing is not None:
            existing.audio_seconds_in = usage.audio_seconds_in
            existing.audio_seconds_out = usage.audio_seconds_out
            existing.provider_characters = usage.provider_characters
            existing.interruptions = usage.interruptions
            existing.time_to_first_audio_ms = latency.time_to_first_audio_ms
            existing.session_duration_ms = latency.session_duration_ms
            existing.recorded_at = now
        else:
            self._session.add(
                VoiceUsageRecordRecord(
                    id=uuid.uuid7(),
                    organization_id=self._tenant.organization_id,
                    session_id=session_id,
                    audio_seconds_in=usage.audio_seconds_in,
                    audio_seconds_out=usage.audio_seconds_out,
                    provider_characters=usage.provider_characters,
                    interruptions=usage.interruptions,
                    time_to_first_audio_ms=latency.time_to_first_audio_ms,
                    session_duration_ms=latency.session_duration_ms,
                    recorded_at=now,
                )
            )
        await self._session.flush()


class VoiceSecretStore:
    """Encrypted-at-rest ciphertext persistence for voice provider credentials (the
    ElevenLabs API key, a webhook secret). Satisfies
    :class:`~nexus_ai.integrations.credentials.EncryptedSecretStore`."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def get(self, organization_id: uuid.UUID, ref: str) -> EncryptedSecret | None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            row = (
                (
                    await tenant.session.execute(
                        select(VoiceSecretRecord).where(
                            VoiceSecretRecord.organization_id == organization_id,
                            VoiceSecretRecord.credential_ref == ref,
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
                        select(VoiceSecretRecord).where(
                            VoiceSecretRecord.organization_id == organization_id,
                            VoiceSecretRecord.credential_ref == ref,
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
                    VoiceSecretRecord(
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
                delete(VoiceSecretRecord).where(
                    VoiceSecretRecord.organization_id == organization_id,
                    VoiceSecretRecord.credential_ref == ref,
                )
            )
            return cast("CursorResult[Any]", result).rowcount > 0
