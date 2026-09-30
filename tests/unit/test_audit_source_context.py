from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from nexus_ai.audit.context import actor_scope, require_actor, source_actor
from nexus_ai.audit.contracts import AuditActor
from nexus_ai.core.context import request_context
from nexus_ai.core.errors import PermissionDeniedError


@pytest.mark.anyio
async def test_actor_is_tenant_scoped_and_does_not_leak_to_child_task() -> None:
    organization_id = uuid4()
    actor = AuditActor(kind="HUMAN", user_id=uuid4())
    with actor_scope(organization_id, actor):
        assert require_actor(organization_id) == actor
        with pytest.raises(PermissionDeniedError):
            require_actor(uuid4())

        async def child() -> None:
            with pytest.raises(PermissionDeniedError):
                require_actor(organization_id)

        await asyncio.create_task(child())
    with pytest.raises(PermissionDeniedError):
        require_actor(organization_id)


@pytest.mark.anyio
async def test_request_without_live_actor_never_becomes_service() -> None:
    with request_context(), pytest.raises(PermissionDeniedError):
        source_actor(uuid4(), service="customer-service")


@pytest.mark.anyio
async def test_explicit_internal_service_and_human_scope() -> None:
    organization_id = uuid4()
    assert source_actor(organization_id, service="customer-service").service == "customer-service"
    actor = AuditActor(kind="HUMAN", user_id=uuid4())
    with request_context(), actor_scope(organization_id, actor):
        assert source_actor(organization_id, service="customer-service") == actor


@pytest.mark.anyio
async def test_bound_actor_cannot_escape_request_lifetime() -> None:
    organization_id = uuid4()
    with (
        request_context(),
        actor_scope(organization_id, AuditActor(kind="HUMAN", user_id=uuid4())),
        request_context(),
        pytest.raises(PermissionDeniedError),
    ):
        require_actor(organization_id)
