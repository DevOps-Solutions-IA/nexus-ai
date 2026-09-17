"""P17 tenant isolation, composite-key and immutable-audit adversarial tests."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from nexus_ai.humans.entities import ClaimWorkRequest, SupervisorTransferToAgentRequest
from nexus_ai.humans.errors import HumanExecutionFencedError, HumanNotFoundError
from tests.integration.test_human_operations_service import _conversation, _ready_work

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_human_rows_are_invisible_across_tenants(
    human_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    tenant_a = await make_organization()
    tenant_b = await make_organization()
    agent_b = await make_tool_principal(tenant_b)
    queue, work, conversation = await _ready_work(human_stack, tenant_b, agent_b)
    claim = await human_stack.service.claim_work(
        tenant_b.id,
        agent_b.user_id,
        ClaimWorkRequest(queue_id=queue.id, expected_queue_revision=queue.revision),
    )
    async with human_stack.database.tenant_transaction(tenant_a.id) as tenant:
        for table in (
            "human_queues",
            "human_agent_presence",
            "human_work_items",
            "conversation_ownership",
            "human_assignments",
            "human_handoffs",
            "human_transition_history",
        ):
            count = (
                await tenant.session.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            ).scalar_one()
            assert count == 0
    with pytest.raises(HumanNotFoundError):
        await human_stack.service.get_queue(tenant_a.id, queue.id)
    with pytest.raises(HumanNotFoundError):
        await human_stack.service.get_work_item(tenant_a.id, work.id)
    with pytest.raises(HumanNotFoundError):
        await human_stack.service.get_ownership(tenant_a.id, conversation.id)
    with pytest.raises((HumanExecutionFencedError, HumanNotFoundError)):
        await human_stack.service.supervisor_transfer_to_agent(
            tenant_a.id,
            claim.assignment.id,
            SupervisorTransferToAgentRequest(
                target_agent_user_id=agent_b.user_id,
                reason_code="CROSS_TENANT_DENIED",
            ),
            actor_user_id=agent_b.user_id,
        )


async def test_cross_tenant_presence_composite_fk_is_database_enforced(
    human_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    tenant_a = await make_organization()
    tenant_b = await make_organization()
    agent_b = await make_tool_principal(tenant_b)
    with pytest.raises(DBAPIError):
        async with human_stack.database.tenant_transaction(tenant_a.id) as tenant:
            await tenant.session.execute(
                text(
                    "INSERT INTO human_agent_presence "
                    "(id, organization_id, agent_user_id, state, capacity, "
                    "active_assignment_count, version) "
                    "VALUES (:id, :organization_id, :agent_user_id, 'AVAILABLE', 1, 0, 1)"
                ),
                {
                    "id": uuid.uuid7(),
                    "organization_id": tenant_a.id,
                    "agent_user_id": agent_b.user_id,
                },
            )


async def test_human_transition_history_is_append_only(
    human_stack: Any, make_organization: Any, make_tool_principal: Any
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    _, work, _ = await _ready_work(human_stack, organization, principal)
    with pytest.raises(DBAPIError):
        async with human_stack.database.tenant_transaction(organization.id) as tenant:
            await tenant.session.execute(
                text(
                    "UPDATE human_transition_history SET reason_code='TAMPERED' "
                    "WHERE entity_id=:entity_id"
                ),
                {"entity_id": work.id},
            )


@pytest.mark.parametrize(
    ("mode", "assignment_id", "agent_user_id", "ai_session_id"),
    (
        ("AI", None, None, None),
        ("HUMAN", None, "agent", None),
        ("UNASSIGNED", None, "agent", None),
    ),
)
async def test_conversation_ownership_invalid_authority_shape_is_database_rejected(
    human_stack: Any,
    make_organization: Any,
    make_tool_principal: Any,
    mode: str,
    assignment_id: str | None,
    agent_user_id: str | None,
    ai_session_id: str | None,
) -> None:
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    _, conversation = await _conversation(human_stack, organization.id)
    with pytest.raises(DBAPIError) as raised:
        async with human_stack.database.tenant_transaction(organization.id) as tenant:
            await tenant.session.execute(
                text(
                    "INSERT INTO conversation_ownership "
                    "(id, organization_id, conversation_id, mode, ownership_generation, "
                    "assignment_id, agent_user_id, ai_session_id) "
                    "VALUES (:id, :organization_id, :conversation_id, :mode, 1, "
                    ":assignment_id, :agent_user_id, :ai_session_id)"
                ),
                {
                    "id": uuid.uuid7(),
                    "organization_id": organization.id,
                    "conversation_id": conversation.id,
                    "mode": mode,
                    "assignment_id": assignment_id,
                    "agent_user_id": principal.user_id if agent_user_id else None,
                    "ai_session_id": ai_session_id,
                },
            )
    assert "ck_conversation_ownership_authority_shape" in str(raised.value)
