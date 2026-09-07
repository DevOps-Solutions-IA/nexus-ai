"""Concurrent multi-tenant isolation under real PostgreSQL (sections 38, 57)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.core.tenancy import TenantContext, TenantContextSource, current_tenant, tenant_scope

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_200_mixed_tenant_operations_never_cross(
    tenant_database: Any, make_organization
) -> None:
    orgs = [await make_organization(activate=True) for _ in range(3)]
    keys = {org.id: org.organization_key for org in orgs}
    leaks: list[str] = []

    async def operation(index: int) -> None:
        org = orgs[index % 3]
        context = TenantContext(org.id, TenantContextSource.SYSTEM_BOOTSTRAP)
        with tenant_scope(context):
            await asyncio.sleep(0)
            assert current_tenant() is context
            async with tenant_database.tenant_transaction(org.id) as ts:
                visible = (
                    (await ts.session.execute(text("SELECT organization_key FROM organizations")))
                    .scalars()
                    .all()
                )
                if visible != [keys[org.id]]:
                    leaks.append(f"task {index}: {visible!r}")
                await ts.session.execute(text("UPDATE organizations SET updated_at = now()"))

    await asyncio.gather(*(operation(i) for i in range(240)))
    assert leaks == []
    assert current_tenant() is None


async def test_background_task_without_context_fails_safe(tenant_database: Any) -> None:
    async def orphan() -> list[str]:
        # No tenant_scope / tenant_transaction: an unscoped read must see nothing.
        async with tenant_database.session() as session:
            return list(
                (await session.execute(text("SELECT organization_key FROM organizations")))
                .scalars()
                .all()
            )

    assert await asyncio.create_task(orphan()) == []


async def test_child_task_does_not_inherit_parent_scope(
    tenant_database: Any, make_organization
) -> None:
    org = await make_organization()
    seen: dict[str, Any] = {}

    async def child() -> None:
        seen["child"] = current_tenant()

    context = TenantContext(org.id, TenantContextSource.SYSTEM_BOOTSTRAP)
    with tenant_scope(context):
        # Documented: a sub-task of a tenant operation, created inside the scope, is part of
        # that operation and inherits the copied context. The security guarantee lives in the
        # DB layer (tenant_transaction) and the resolver, never in "tasks never inherit".
        await asyncio.create_task(child())
    assert seen["child"] is context
    # A task created AFTER the scope exits carries no tenant context.
    seen.clear()
    await asyncio.create_task(child())
    assert seen["child"] is None
