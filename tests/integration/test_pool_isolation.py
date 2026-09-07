"""Pooled connection reuse must never leak tenant scope (sections 19, 56)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

_KEYS = text("SELECT organization_key FROM organizations")


async def _visible(session: Any) -> list[str]:
    return list((await session.execute(_KEYS)).scalars().all())


async def test_commit_then_reuse_pool_size_one(pool1_database: Any, make_organization) -> None:
    a = await make_organization()
    b = await make_organization()
    async with pool1_database.tenant_transaction(a.id) as ts:
        assert await _visible(ts.session) == [a.organization_key]
    # Same physical connection (pool_size=1) now serves B.
    async with pool1_database.tenant_transaction(b.id) as ts:
        visible = await _visible(ts.session)
        assert visible == [b.organization_key]
        assert a.organization_key not in visible


async def test_rollback_then_reuse(pool1_database: Any, make_organization) -> None:
    a = await make_organization()
    b = await make_organization()
    try:
        async with pool1_database.tenant_transaction(a.id) as ts:
            await ts.session.execute(text("UPDATE organizations SET display_name = 'x'"))
            raise RuntimeError("force rollback")
    except RuntimeError:
        pass
    async with pool1_database.tenant_transaction(b.id) as ts:
        assert await _visible(ts.session) == [b.organization_key]


async def test_exception_inside_scope_then_reuse(pool1_database: Any, make_organization) -> None:
    a = await make_organization()
    b = await make_organization()
    with pytest.raises(ZeroDivisionError):
        async with pool1_database.tenant_transaction(a.id):
            _ = 1 / 0
    async with pool1_database.tenant_transaction(b.id) as ts:
        assert await _visible(ts.session) == [b.organization_key]
    # A plain (unscoped) session on the reused connection sees nothing.
    async with pool1_database.session() as session:
        assert await _visible(session) == []


async def test_no_setting_survives_between_transactions(
    pool1_database: Any, make_organization
) -> None:
    a = await make_organization()
    async with pool1_database.tenant_transaction(a.id):
        pass
    async with pool1_database.session() as session:
        leaked = (
            await session.execute(text("SELECT current_setting('nxs.organization_id', true)"))
        ).scalar_one_or_none()
    assert leaked in (None, "")
