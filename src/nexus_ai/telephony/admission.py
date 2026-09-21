"""Durable pre-ARI owner fencing; never retries an external dispatch."""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select

from nexus_ai.domain.sip_edge.models import SipCallAdmissionRecord, SipEgressPermitRecord
from nexus_ai.domain.telephony.repository import TelephonyCallRepository
from nexus_ai.infrastructure.tenant_session import TenantSession
from nexus_ai.sip_edge.errors import SipRouteDeniedError
from nexus_ai.telephony.entities import Call, CallState
from nexus_ai.telephony.state_machine import is_terminal, state_rank


async def establish(tenant: TenantSession, call_id: UUID, owner_id: UUID) -> None:
    now = (await tenant.session.execute(select(func.clock_timestamp()))).scalar_one()
    tenant.session.add(
        SipCallAdmissionRecord(
            organization_id=tenant.organization_id,
            call_id=call_id,
            owner_id=owner_id,
            state="PENDING",
            created_at=now,
            expires_at=now + timedelta(seconds=30),
        )
    )
    await tenant.session.flush()


async def fence(
    tenant: TenantSession,
    call_id: UUID,
    *,
    owner_id: UUID | None = None,
    dispatch: bool = False,
) -> Call | None:
    repo = TelephonyCallRepository(tenant)
    call = await repo.by_id(call_id, for_update=True)
    admission = (
        await tenant.session.execute(
            select(SipCallAdmissionRecord)
            .where(SipCallAdmissionRecord.call_id == call_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if admission is None or call is None or admission.state != "PENDING":
        if dispatch:
            raise SipRouteDeniedError()
        return None
    now = (await tenant.session.execute(select(func.clock_timestamp()))).scalar_one()
    if owner_id is not None and admission.owner_id != owner_id:
        raise SipRouteDeniedError()
    if not dispatch and owner_id is None and admission.expires_at > now:
        return None
    permit = (
        await tenant.session.execute(
            select(SipEgressPermitRecord)
            .where(SipEgressPermitRecord.call_id == call_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if dispatch:
        if (
            admission.expires_at <= now
            or is_terminal(call.state)
            or permit is None
            or permit.state != "AUTHORIZED"
            or permit.expires_at <= now
        ):
            raise SipRouteDeniedError()
        admission.state = "DISPATCHED"
        await repo.apply(call_id, {"error_code": "NXS_TELEPHONY_PROVIDER_DISPATCH_UNCONFIRMED"})
        await tenant.session.flush()
        return None
    admission.state = "REVOKED"
    if permit is not None:
        if permit.state not in {"AUTHORIZED", "REVOKED", "EXPIRED"}:
            raise SipRouteDeniedError()
        if permit.state == "AUTHORIZED":
            permit.state = "REVOKED"
    if is_terminal(call.state):
        await tenant.session.flush()
        return None
    updated = await repo.apply(
        call_id,
        {
            "state": CallState.FAILED.value,
            "state_rank": state_rank(CallState.FAILED),
            "disposition": "FAILED",
            "error_code": "NXS_TELEPHONY_ADMISSION_FAILED",
            "ended_at": now,
        },
    )
    await tenant.session.flush()
    return updated
