"""Real PostgreSQL discovery isolation and canonical P11 revalidation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from nexus_ai.core.config import DatabaseSettings
from nexus_ai.domain.sip_edge.models import SipDidLocatorRecord
from nexus_ai.infrastructure.database import Database
from nexus_ai.sip_edge.errors import SipRouteDeniedError
from nexus_ai.sip_edge.locator import DidLocator, revalidate_source
from nexus_ai.telephony.entities import (
    AccountStatus,
    CreateAccountRequest,
    RegisterPhoneNumberRequest,
)
from tests.conftest import RUNTIME_DSN

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.fixture
async def discovery_database(tenant_database: Any) -> AsyncIterator[Database]:
    local_secret = "local-sip-locator-only"
    dsn = make_url(RUNTIME_DSN).set(username="nexus_sip_locator", password=local_secret)
    database = Database(DatabaseSettings(dsn=SecretStr(dsn.render_as_string(hide_password=False))))
    await database.connect()
    try:
        yield database
    finally:
        await database.disconnect()


async def provision(stack: Any, organization: Any, number: str) -> Any:
    account = await stack.service.create_account(
        organization.id,
        CreateAccountRequest(
            provider="fake",
            slug=f"sip-{uuid4().hex}",
            external_account_id=uuid4().hex,
            configuration={"default_country": "1"},
        ),
    )
    registered = await stack.service.register_number(
        organization.id,
        RegisterPhoneNumberRequest(account_id=account.id, e164=number, inbound_enabled=True),
    )
    await stack.service.set_number_verified(organization.id, registered.id, verified=True)
    return account, registered


async def test_shared_carrier_more_than_32_tenants(
    telephony_stack: Any, make_organization: Any, discovery_database: Database
) -> None:
    locator = DidLocator(discovery_database)
    for index in range(33):
        organization = await make_organization()
        account, number = await provision(telephony_stack, organization, f"+1202555{index:04}")
        candidate = await locator.discover(number.e164)
        assert candidate.organization_id == organization.id
        assert candidate.account_id == account.id
        assert candidate.phone_number_id == number.id
        async with telephony_stack.database.tenant_transaction(organization.id) as tenant:
            await revalidate_source(tenant, candidate)


async def test_stale_disabled_and_foreign_candidates_deny(
    telephony_stack: Any, make_organization: Any, discovery_database: Database
) -> None:
    organization = await make_organization()
    foreign = await make_organization()
    account, number = await provision(telephony_stack, organization, "+12025550190")
    locator = DidLocator(discovery_database)
    candidate = await locator.discover(number.e164)
    async with telephony_stack.database.tenant_transaction(foreign.id) as tenant:
        with pytest.raises(SipRouteDeniedError):
            await revalidate_source(tenant, candidate)
        assert (await tenant.session.execute(select(SipDidLocatorRecord))).scalars().all() == []
    for changes in (
        {"phone_number_id": uuid4()},
        {"account_id": uuid4()},
        {"revision": candidate.revision + 1},
        {"locator_id": uuid4()},
    ):
        async with telephony_stack.database.tenant_transaction(organization.id) as tenant:
            with pytest.raises(SipRouteDeniedError):
                await revalidate_source(tenant, candidate.model_copy(update=changes))
    await telephony_stack.service.set_account_status(
        organization.id, account.id, AccountStatus.DISABLED
    )
    with pytest.raises(SipRouteDeniedError):
        await locator.discover(number.e164)
    async with telephony_stack.database.tenant_transaction(organization.id) as tenant:
        with pytest.raises(SipRouteDeniedError):
            await revalidate_source(tenant, candidate)
    await telephony_stack.service.set_account_status(
        organization.id, account.id, AccountStatus.ACTIVE
    )
    refreshed = await locator.discover(number.e164)
    assert refreshed.revision > candidate.revision
    async with telephony_stack.database.tenant_transaction(organization.id) as tenant:
        with pytest.raises(SipRouteDeniedError):
            await revalidate_source(tenant, candidate)
        await revalidate_source(tenant, refreshed)


async def test_discovery_role_cannot_read_tenant_tables_or_list(
    telephony_stack: Any, make_organization: Any, discovery_database: Database
) -> None:
    organization = await make_organization()
    await provision(telephony_stack, organization, "+12025550191")
    role = await discovery_database.runtime_role_report()
    assert not role.can_bypass_tenancy and not role.can_create_role
    async with discovery_database.transaction() as session:
        await session.execute(
            text("SELECT set_config('nxs.organization_id', :organization, true)"),
            {"organization": str(organization.id)},
        )
        assert (
            await session.execute(text("SELECT count(*) FROM sip_did_locators"))
        ).scalar_one() == 0
    for query in (
        "SELECT * FROM organizations",
        "SELECT * FROM telephony_phone_numbers",
        "SELECT * FROM telephony_accounts",
    ):
        with pytest.raises(DBAPIError):
            async with discovery_database.transaction() as session:
                await session.execute(text(query))


async def test_source_and_projection_rollback_together(
    telephony_stack: Any, make_organization: Any, discovery_database: Database
) -> None:
    organization = await make_organization()
    account, number = await provision(telephony_stack, organization, "+12025550192")
    locator = DidLocator(discovery_database)
    original = await locator.discover(number.e164)
    with pytest.raises(RuntimeError, match="rollback"):
        async with telephony_stack.database.tenant_transaction(organization.id) as tenant:
            await tenant.session.execute(
                text("UPDATE telephony_accounts SET status = 'DISABLED' WHERE id = :account"),
                {"account": account.id},
            )
            assert not (
                await tenant.session.execute(select(SipDidLocatorRecord.active))
            ).scalar_one()
            raise RuntimeError("rollback")
    assert await locator.discover(number.e164) == original


async def test_concurrent_account_disable_and_number_verification(
    telephony_stack: Any, make_organization: Any, discovery_database: Database
) -> None:
    organization = await make_organization()
    account, number = await provision(telephony_stack, organization, "+12025550193")
    barrier = asyncio.Barrier(2)

    async def disable() -> None:
        await barrier.wait()
        await telephony_stack.service.set_account_status(
            organization.id, account.id, AccountStatus.DISABLED
        )

    async def verify() -> None:
        await barrier.wait()
        await telephony_stack.service.set_number_verified(organization.id, number.id, verified=True)

    async with asyncio.timeout(10):
        await asyncio.gather(disable(), verify())
    with pytest.raises(SipRouteDeniedError):
        await DidLocator(discovery_database).discover(number.e164)
    async with telephony_stack.database.tenant_transaction(organization.id) as tenant:
        assert not (await tenant.session.execute(select(SipDidLocatorRecord.active))).scalar_one()


async def test_foreign_locator_mutation_and_raw_source_forgery_denied(
    telephony_stack: Any, make_organization: Any, discovery_database: Database
) -> None:
    organization = await make_organization()
    foreign = await make_organization()
    _, number = await provision(telephony_stack, organization, "+12025550194")
    original = await DidLocator(discovery_database).discover(number.e164)
    async with telephony_stack.database.tenant_transaction(foreign.id) as tenant:
        result = await tenant.session.execute(
            text("UPDATE sip_did_locators SET active=false,revision=revision+1 WHERE id=:id"),
            {"id": original.locator_id},
        )
        assert result.rowcount == 0
    for statement in (
        "UPDATE sip_did_locators SET organization_id=:foreign,revision=revision+1 WHERE id=:id",
        "UPDATE sip_did_locators SET account_id=:foreign,revision=revision+1 WHERE id=:id",
        "UPDATE sip_did_locators SET phone_number_id=:foreign,revision=revision+1 WHERE id=:id",
        "UPDATE sip_did_locators SET active=false,revision=revision+1 WHERE id=:id",
    ):
        with pytest.raises(DBAPIError):
            async with telephony_stack.database.tenant_transaction(organization.id) as tenant:
                await tenant.session.execute(
                    text(statement),
                    {
                        "foreign": foreign.id,
                        "id": original.locator_id,
                    },
                )
    assert await DidLocator(discovery_database).discover(number.e164) == original


async def test_deleted_source_recreation_does_not_revive_locator_identity(
    telephony_stack: Any, make_organization: Any, discovery_database: Database
) -> None:
    organization = await make_organization()
    account, number = await provision(telephony_stack, organization, "+12025550195")
    locator = DidLocator(discovery_database)
    original = await locator.discover(number.e164)
    async with telephony_stack.database.tenant_transaction(organization.id) as tenant:
        await tenant.session.execute(
            text("DELETE FROM telephony_phone_numbers WHERE id=:id"), {"id": number.id}
        )
    with pytest.raises(SipRouteDeniedError):
        await locator.discover(number.e164)
    replacement = await telephony_stack.service.register_number(
        organization.id,
        RegisterPhoneNumberRequest(account_id=account.id, e164=number.e164, inbound_enabled=True),
    )
    await telephony_stack.service.set_number_verified(
        organization.id, replacement.id, verified=True
    )
    current = await locator.discover(number.e164)
    assert current.locator_id != original.locator_id
    assert current.phone_number_id != original.phone_number_id
    async with telephony_stack.database.tenant_transaction(organization.id) as tenant:
        with pytest.raises(SipRouteDeniedError):
            await revalidate_source(tenant, original)
        await revalidate_source(tenant, current)
