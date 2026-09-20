"""Bounded rollout never recreates ownership or overwrites a current projection."""

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text

from nexus_ai.core.errors import PermissionDeniedError
from nexus_ai.sip_edge.backfill import BackfillBatch, backfill_locators
from nexus_ai.sip_edge.errors import SipRouteDeniedError
from nexus_ai.sip_edge.locator import DidLocator
from tests.integration.test_sip_did_locator import discovery_database as discovery_database
from tests.integration.test_sip_did_locator import provision
from tests.integration.test_sip_target_constraints import target_control as target_control

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_explicit_backfill_is_bounded_idempotent_and_tenant_scoped(
    target_control: Any,
    tenant_database: Any,
    make_organization: Any,
    telephony_stack: Any,
    discovery_database: Any,
) -> None:
    _, actor = target_control
    organization = await make_organization()
    account, number = await provision(telephony_stack, organization, "+12025550870")
    locator = DidLocator(discovery_database)
    original = await locator.discover(number.e164)
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        await tenant.session.execute(text("DELETE FROM sip_did_locators"))
    with pytest.raises(SipRouteDeniedError):
        await locator.discover(number.e164)
    request = BackfillBatch(organization_id=organization.id, account_id=account.id, limit=1)
    with pytest.raises(PermissionDeniedError):
        await backfill_locators(tenant_database, uuid4(), request)
    result = await backfill_locators(tenant_database, actor, request)
    assert result.examined == result.created == 1
    assert result.next_number_id == number.id
    restored = await locator.discover(number.e164)
    assert restored.locator_id != original.locator_id
    assert restored.phone_number_id == original.phone_number_id
    replay = await backfill_locators(tenant_database, actor, request)
    assert replay.created == 0
    assert await locator.discover(number.e164) == restored
    final = await backfill_locators(
        tenant_database, actor, request.model_copy(update={"after_number_id": number.id})
    )
    assert final.examined == 0 and final.next_number_id is None
    foreign = await make_organization()
    with pytest.raises(SipRouteDeniedError):
        await backfill_locators(
            tenant_database, actor, request.model_copy(update={"organization_id": foreign.id})
        )
