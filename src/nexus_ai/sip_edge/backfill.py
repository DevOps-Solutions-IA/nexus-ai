"""Explicit tenant/account keyset batches for pre-P19 P11 number discovery."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID, uuid7

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from nexus_ai.domain.sip_edge.models import SipDidLocatorRecord
from nexus_ai.domain.telephony.models import TelephonyAccountRecord, TelephonyPhoneNumberRecord
from nexus_ai.infrastructure.database import Database
from nexus_ai.sip_edge.contracts import StrictContract
from nexus_ai.sip_edge.errors import SipRouteDeniedError
from nexus_ai.sip_edge.targets import authorize_control


class BackfillBatch(StrictContract):
    organization_id: UUID
    account_id: UUID
    after_number_id: UUID | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50


class BackfillResult(StrictContract):
    examined: int
    created: int
    next_number_id: UUID | None


async def backfill_locators(
    database: Database, actor_user_id: UUID, request: BackfillBatch
) -> BackfillResult:
    request = BackfillBatch.model_validate(request.model_dump())
    async with database.tenant_transaction(request.organization_id) as tenant:
        await authorize_control(tenant.session, actor_user_id)
        account = (
            await tenant.session.execute(
                select(TelephonyAccountRecord)
                .where(TelephonyAccountRecord.id == request.account_id)
                .with_for_update(read=True)
            )
        ).scalar_one_or_none()
        if account is None:
            raise SipRouteDeniedError()
        query = select(TelephonyPhoneNumberRecord).where(
            TelephonyPhoneNumberRecord.account_id == account.id
        )
        if request.after_number_id is not None:
            query = query.where(TelephonyPhoneNumberRecord.id > request.after_number_id)
        numbers = (
            (
                await tenant.session.execute(
                    query.order_by(TelephonyPhoneNumberRecord.id)
                    .limit(request.limit)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        created = 0
        for number in numbers:
            identifier = (
                await tenant.session.execute(
                    insert(SipDidLocatorRecord)
                    .values(
                        id=uuid7(),
                        organization_id=request.organization_id,
                        phone_number_id=number.id,
                        account_id=account.id,
                        e164=number.e164,
                        revision=1,
                        active=number.verified
                        and number.inbound_enabled
                        and account.status == "ACTIVE",
                    )
                    .on_conflict_do_nothing(index_elements=["phone_number_id"])
                    .returning(SipDidLocatorRecord.id)
                )
            ).scalar_one_or_none()
            created += identifier is not None
        return BackfillResult(
            examined=len(numbers),
            created=created,
            next_number_id=numbers[-1].id if len(numbers) == request.limit else None,
        )
