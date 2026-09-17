"""Fail-closed P17 authority, secret-safety and provider-boundary tests."""

from __future__ import annotations

import inspect
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.humans.errors import HumanExecutionFencedError
from nexus_ai.humans.service import HumanOperationsService
from tests.integration.test_human_operations_service import _claimed, _ready_work

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_claim_token_never_enters_p04_outbox(
    human_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    claim, _ = await _claimed(human_stack, organization, principal)
    async with human_stack.database.tenant_transaction(organization.id) as tenant:
        exposed = (
            await tenant.session.execute(
                text("SELECT count(*) FROM event_outbox WHERE envelope::text LIKE :needle"),
                {"needle": f"%{claim.claim_token}%"},
            )
        ).scalar_one()
    assert exposed == 0


async def test_ai_to_human_generation_fences_stale_ai_output(
    human_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    _, _, conversation = await _ready_work(human_stack, organization, principal)
    ownership = await human_stack.service.get_ownership(organization.id, conversation.id)
    with pytest.raises(HumanExecutionFencedError):
        await human_stack.service.validate_ai_output_authority(
            organization.id,
            conversation.id,
            ownership_generation=ownership.ownership_generation - 1,
            ai_session_id=uuid.uuid7(),
        )


def test_p17_has_no_direct_provider_or_network_client_boundary() -> None:
    source = inspect.getsource(HumanOperationsService)
    assert "self._messaging.send(" in source
    assert "self._agents.start_session(" in source
    for forbidden in (
        "httpx.",
        "requests.",
        "Twilio",
        "WhatsAppProvider",
        "EmailProvider",
        "SmsProvider",
        "subprocess.",
    ):
        assert forbidden not in source
