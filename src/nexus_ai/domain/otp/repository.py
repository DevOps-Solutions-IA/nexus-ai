"""OTP challenge persistence (NXS-P10).

Every repository is tenant-scoped: constructed with a ``TenantSession`` so every query is
RLS-confined whatever the WHERE clause says, and the composite tenant-aware FKs refuse a
cross-tenant binding at the database. Verification takes a row lock
(``SELECT ... FOR UPDATE``) so concurrent submissions on one challenge serialize and
exactly one can succeed.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import delete, func, select, update

from nexus_ai.domain.otp.models import OtpChallengeRecord
from nexus_ai.infrastructure.tenant_session import TenantSession
from nexus_ai.otp.entities import (
    OtpChallenge,
    OtpChallengeCreate,
    OtpChannel,
    OtpStatus,
    OtpSubjectType,
)

_ACTIVE = OtpStatus.ACTIVE.value


def _to_challenge(row: OtpChallengeRecord) -> OtpChallenge:
    return OtpChallenge(
        id=row.id,
        organization_id=row.organization_id,
        subject_type=OtpSubjectType(row.subject_type),
        destination=row.destination,
        destination_fingerprint=row.destination_fingerprint,
        purpose=row.purpose,
        channel=OtpChannel(row.channel),
        messaging_account_id=row.messaging_account_id,
        code_hash=row.code_hash,
        hash_version=row.hash_version,
        status=OtpStatus(row.status),
        attempts=row.attempts,
        max_attempts=row.max_attempts,
        request_fingerprint=row.request_fingerprint,
        idempotency_key=row.idempotency_key,
        delivery_message_id=row.delivery_message_id,
        correlation_id=row.correlation_id,
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        resend_after=row.resend_after,
        verified_at=row.verified_at,
        revoked_at=row.revoked_at,
        locked_at=row.locked_at,
        last_attempt_at=row.last_attempt_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class OtpChallengeRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self._tenant = tenant
        self._session = tenant.session

    async def insert(self, create: OtpChallengeCreate) -> OtpChallenge:
        record = OtpChallengeRecord(
            id=create.id,
            organization_id=self._tenant.organization_id,
            subject_type=create.subject_type.value,
            destination=create.destination,
            destination_fingerprint=create.destination_fingerprint,
            purpose=create.purpose,
            channel=create.channel.value,
            messaging_account_id=create.messaging_account_id,
            code_hash=create.code_hash,
            hash_version=create.hash_version,
            status=OtpStatus.ACTIVE.value,
            attempts=0,
            max_attempts=create.max_attempts,
            request_fingerprint=create.request_fingerprint,
            idempotency_key=create.idempotency_key,
            delivery_message_id=None,
            correlation_id=create.correlation_id,
            issued_at=create.issued_at,
            expires_at=create.expires_at,
            resend_after=create.resend_after,
        )
        self._session.add(record)
        await self._session.flush()
        await self._session.refresh(record)
        return _to_challenge(record)

    async def by_id(
        self, challenge_id: uuid.UUID, *, for_update: bool = False
    ) -> OtpChallenge | None:
        query = select(OtpChallengeRecord).where(
            OtpChallengeRecord.organization_id == self._tenant.organization_id,
            OtpChallengeRecord.id == challenge_id,
        )
        if for_update:
            query = query.with_for_update()
        row = (await self._session.execute(query)).scalars().one_or_none()
        return None if row is None else _to_challenge(row)

    async def by_idempotency_key(self, idempotency_key: str) -> OtpChallenge | None:
        row = (
            (
                await self._session.execute(
                    select(OtpChallengeRecord).where(
                        OtpChallengeRecord.organization_id == self._tenant.organization_id,
                        OtpChallengeRecord.idempotency_key == idempotency_key,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )
        return None if row is None else _to_challenge(row)

    async def active_for_subject(
        self, destination_fingerprint: str, purpose: str, *, for_update: bool = False
    ) -> OtpChallenge | None:
        query = select(OtpChallengeRecord).where(
            OtpChallengeRecord.organization_id == self._tenant.organization_id,
            OtpChallengeRecord.destination_fingerprint == destination_fingerprint,
            OtpChallengeRecord.purpose == purpose,
            OtpChallengeRecord.status == _ACTIVE,
        )
        if for_update:
            query = query.with_for_update()
        row = (await self._session.execute(query)).scalars().one_or_none()
        return None if row is None else _to_challenge(row)

    async def count_issued_since(
        self, destination_fingerprint: str, purpose: str, since: dt.datetime
    ) -> int:
        return int(
            (
                await self._session.execute(
                    select(func.count())
                    .select_from(OtpChallengeRecord)
                    .where(
                        OtpChallengeRecord.organization_id == self._tenant.organization_id,
                        OtpChallengeRecord.destination_fingerprint == destination_fingerprint,
                        OtpChallengeRecord.purpose == purpose,
                        OtpChallengeRecord.issued_at >= since,
                    )
                )
            ).scalar_one()
        )

    async def apply(self, challenge_id: uuid.UUID, changes: dict[str, Any]) -> OtpChallenge | None:
        row = (
            (
                await self._session.execute(
                    update(OtpChallengeRecord)
                    .where(
                        OtpChallengeRecord.organization_id == self._tenant.organization_id,
                        OtpChallengeRecord.id == challenge_id,
                    )
                    .values(**changes, updated_at=dt.datetime.now(dt.UTC))
                    .returning(OtpChallengeRecord)
                )
            )
            .scalars()
            .one_or_none()
        )
        return None if row is None else _to_challenge(row)

    async def purge_terminal_before(self, cutoff: dt.datetime) -> int:
        """Cleanup seam (NXS-P15 owns scheduling). Deletes challenges that reached a
        terminal state before ``cutoff``; ACTIVE challenges are never purged."""
        result = await self._session.execute(
            delete(OtpChallengeRecord)
            .where(
                OtpChallengeRecord.organization_id == self._tenant.organization_id,
                OtpChallengeRecord.status != _ACTIVE,
                OtpChallengeRecord.updated_at < cutoff,
            )
            .returning(OtpChallengeRecord.id)
        )
        return len(result.scalars().all())
