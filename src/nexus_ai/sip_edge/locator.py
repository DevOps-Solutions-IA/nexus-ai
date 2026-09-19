"""Exact-key discovery followed by tenant-confined P11 authority checks."""

from __future__ import annotations

from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as DatabaseTimeoutError

from nexus_ai.cells.contracts import Generation
from nexus_ai.core.errors import ConfigurationError
from nexus_ai.domain.organizations.models import OrganizationRecord
from nexus_ai.domain.sip_edge.models import SipDidLocatorRecord
from nexus_ai.domain.telephony.models import TelephonyAccountRecord, TelephonyPhoneNumberRecord
from nexus_ai.infrastructure.database import Database
from nexus_ai.infrastructure.tenant_session import TenantSession
from nexus_ai.sip_edge.contracts import E164, StrictContract
from nexus_ai.sip_edge.errors import SipRouteDeniedError, SipRouteUnavailableError


class DidCandidate(StrictContract):
    locator_id: UUID
    revision: Generation
    organization_id: UUID
    phone_number_id: UUID
    account_id: UUID
    e164: E164


class DidLocator:
    def __init__(self, discovery_database: Database) -> None:
        self._discovery = discovery_database

    async def discover(self, e164: str) -> DidCandidate:
        number = TypeAdapter(E164).validate_python(e164)
        try:
            role = await self._discovery.runtime_role_report()
            if (
                role.role != "nexus_sip_locator"
                or role.can_bypass_tenancy
                or role.can_create_role
                or role.can_create_db
            ):
                raise SipRouteUnavailableError()
            async with self._discovery.transaction() as session:
                await session.execute(
                    text("SELECT set_config('nxs.sip_lookup_e164', :number, true)"),
                    {"number": number},
                )
                row = (
                    await session.execute(
                        select(SipDidLocatorRecord).where(
                            SipDidLocatorRecord.e164 == number,
                            SipDidLocatorRecord.active.is_(True),
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    raise SipRouteDeniedError()
                return DidCandidate(
                    locator_id=row.id,
                    revision=row.revision,
                    organization_id=row.organization_id,
                    phone_number_id=row.phone_number_id,
                    account_id=row.account_id,
                    e164=row.e164,
                )
        except DBAPIError, DatabaseTimeoutError, ConfigurationError:
            raise SipRouteUnavailableError() from None


async def revalidate_source(tenant: TenantSession, candidate: DidCandidate) -> None:
    """Must run after outer Cell/placement admission in the authorization transaction."""
    if tenant.organization_id != candidate.organization_id:
        raise SipRouteDeniedError()
    session = tenant.session
    organization = (
        await session.execute(
            select(OrganizationRecord)
            .where(OrganizationRecord.id == tenant.organization_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    account = (
        await session.execute(
            select(TelephonyAccountRecord)
            .where(
                TelephonyAccountRecord.organization_id == tenant.organization_id,
                TelephonyAccountRecord.id == candidate.account_id,
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    number = (
        await session.execute(
            select(TelephonyPhoneNumberRecord)
            .where(
                TelephonyPhoneNumberRecord.organization_id == tenant.organization_id,
                TelephonyPhoneNumberRecord.id == candidate.phone_number_id,
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    locator = (
        await session.execute(
            select(SipDidLocatorRecord)
            .where(
                SipDidLocatorRecord.organization_id == tenant.organization_id,
                SipDidLocatorRecord.id == candidate.locator_id,
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if (
        organization is None
        or organization.status != "ACTIVE"
        or account is None
        or account.status != "ACTIVE"
        or number is None
        or number.account_id != candidate.account_id
        or number.e164 != candidate.e164
        or not number.verified
        or not number.inbound_enabled
        or locator is None
        or not locator.active
        or locator.revision != candidate.revision
        or locator.e164 != candidate.e164
        or locator.phone_number_id != candidate.phone_number_id
        or locator.account_id != candidate.account_id
    ):
        raise SipRouteDeniedError()
