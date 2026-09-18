"""Actual P09 dispatch counts and durable identities across placement fences."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text

from nexus_ai.cells.admission import placement_route
from nexus_ai.cells.errors import PlacementFencedError, PlacementRequiredError
from nexus_ai.messaging.entities import (
    MessageChannel,
    MessageContent,
    OutboundAddressInput,
    SendMessageRequest,
)
from nexus_ai.messaging.errors import MessagingSendInProgressError
from tests.integration.test_cell_execution import _transition
from tests.integration.test_cell_placement import _assign
from tests.integration.test_cell_placement import placement_setup as placement_setup
from tests.integration.test_human_operations_service import _conversation
from tests.integration.test_messaging_channels import _account

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _message(stack: Any, organization_id: Any) -> SendMessageRequest:
    _, conversation = await _conversation(SimpleNamespace(messaging=stack), organization_id)
    account = await _account(
        stack,
        organization_id,
        MessageChannel.SMS,
        sender="+14155550100",
        secret={"api_token": "test-token"},
    )
    return SendMessageRequest(
        account_id=account.id,
        conversation_id=conversation.id,
        to=(OutboundAddressInput(value="+14155550101"),),
        content=MessageContent(text="approved"),
        idempotency_key="placement-send",
    )


@pytest.mark.parametrize(
    "fence", ["missing", "suspended", "wrong_cell", "stale_generation", "foreign_org"]
)
async def test_p09_placement_denial_has_zero_provider_calls(
    placement_setup: Any, messaging_stack: Any, tenant_database: Any, monkeypatch: Any, fence: str
) -> None:
    organization, actor, service, cell_id = placement_setup
    request = await _message(messaging_stack, organization.id)
    monkeypatch.setattr(tenant_database, "_worker_cell_id", cell_id)
    if fence == "missing":
        with pytest.raises(PlacementRequiredError):
            await messaging_stack.service.send(organization.id, actor.user_id, request)
    else:
        expected = await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
        if fence == "suspended":
            await _transition(placement_setup, "SUSPEND", 1)
        elif fence == "wrong_cell":
            expected = expected.model_copy(update={"cell_id": uuid4()})
        elif fence == "foreign_org":
            expected = expected.model_copy(update={"organization_id": uuid4()})
        else:
            await _transition(placement_setup, "SUSPEND", 1)
            await _transition(placement_setup, "RESUME", 2)
        with placement_route(expected), pytest.raises(PlacementFencedError):
            await messaging_stack.service.send(organization.id, actor.user_id, request)
    assert messaging_stack.transport.requests == []
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        assert (
            await tenant.session.execute(text("SELECT count(*) FROM messaging_send_idempotency"))
        ).scalar_one() == 0
        assert (
            await tenant.session.execute(text("SELECT count(*) FROM messaging_messages"))
        ).scalar_one() == 0


async def test_p09_authorized_first_completes_after_suspension(
    placement_setup: Any, messaging_stack: Any, tenant_database: Any, monkeypatch: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    monkeypatch.setattr(tenant_database, "_worker_cell_id", cell_id)
    await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    request = await _message(messaging_stack, organization.id)
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def provider(entry: Any) -> Any:
        entered.set()
        await finish.wait()
        return 200, {"message_id": "authorized-before-suspend"}

    messaging_stack.transport.set_handler(provider)
    task = asyncio.create_task(
        messaging_stack.service.send(organization.id, actor.user_id, request)
    )
    try:
        async with asyncio.timeout(10):
            await entered.wait()
            await _transition(placement_setup, "SUSPEND", 1)
            finish.set()
            result = await task
    finally:
        finish.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert len(messaging_stack.transport.requests) == 1
    await _transition(placement_setup, "RESUME", 2)
    replay = await messaging_stack.service.send(organization.id, actor.user_id, request)
    assert result.id == replay.id
    assert len(messaging_stack.transport.requests) == 1


async def test_worker_death_after_queued_authority_never_mints_new_identity(
    placement_setup: Any, messaging_stack: Any, tenant_database: Any, monkeypatch: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    monkeypatch.setattr(tenant_database, "_worker_cell_id", cell_id)
    await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    request = await _message(messaging_stack, organization.id)

    async def disappear(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("worker disappeared after durable message commit")

    with monkeypatch.context() as patch:
        patch.setattr(messaging_stack.service, "_resolve_secret", disappear)
        with pytest.raises(RuntimeError, match="worker disappeared"):
            await messaging_stack.service.send(organization.id, actor.user_id, request)
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        original = (
            await tenant.session.execute(text("SELECT id FROM messaging_messages"))
        ).scalar_one()
    await _transition(placement_setup, "SUSPEND", 1)
    await _transition(placement_setup, "RESUME", 2)
    with pytest.raises(MessagingSendInProgressError):
        await messaging_stack.service.send(organization.id, actor.user_id, request)
    assert messaging_stack.transport.requests == []
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        assert (
            await tenant.session.execute(text("SELECT id FROM messaging_messages"))
        ).scalars().all() == [original]
        assert (
            await tenant.session.execute(text("SELECT count(*) FROM messaging_send_idempotency"))
        ).scalar_one() == 1


async def test_keyed_p09_claim_first_remains_authorized_before_queue_write(
    placement_setup: Any, messaging_stack: Any, tenant_database: Any, monkeypatch: Any
) -> None:
    organization, actor, service, cell_id = placement_setup
    monkeypatch.setattr(tenant_database, "_worker_cell_id", cell_id)
    await service.mutate(organization.id, actor.user_id, _assign(cell_id), uuid4())
    request = await _message(messaging_stack, organization.id)
    original = messaging_stack.service._persist_queued

    async def suspend_after_claim(*args: Any, **kwargs: Any) -> Any:
        async with tenant_database.tenant_transaction(organization.id) as tenant:
            assert (
                await tenant.session.execute(
                    text("SELECT count(*) FROM messaging_send_idempotency WHERE status = 'PENDING'")
                )
            ).scalar_one() == 1
        await _transition(placement_setup, "SUSPEND", 1)
        return await original(*args, **kwargs)

    monkeypatch.setattr(messaging_stack.service, "_persist_queued", suspend_after_claim)
    result = await messaging_stack.service.send(organization.id, actor.user_id, request)
    assert result.id is not None
    assert len(messaging_stack.transport.requests) == 1
