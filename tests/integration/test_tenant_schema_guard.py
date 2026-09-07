"""The tenant schema guard is a permanent architectural gate (sections 28, 93)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import Column, Integer, String, Table, text

from nexus_ai.infrastructure.orm import (
    TENANT_OWNED,
    TENANT_SCOPED_KEY,
    metadata,
)
from nexus_ai.infrastructure.schema_guard import check_live, check_static

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_live_guard_passes_for_migrated_schema(tenant_database: Any) -> None:
    async with tenant_database.engine.connect() as connection:
        result = await check_live(connection)
    assert result.ok, result.violations
    # Every tenant-owned table (self-scoped and mixin-based) is covered, not just
    # ``organizations`` — the guard reads the ORM class hierarchy, not only table.info.
    assert set(result.tables_checked) >= {
        "organizations",
        "memberships",
        "refresh_sessions",
        "role_assignments",
        "event_outbox",
        "event_dead_letters",
    }
    assert "consumer_receipts" not in result.tables_checked


def test_static_guard_flags_unsafe_tenant_table() -> None:
    unsafe = Table(
        "guard_probe_unsafe",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String(10)),
        info={TENANT_SCOPED_KEY: TENANT_OWNED},
    )
    try:
        result = check_static()
        assert not result.ok
        assert any("guard_probe_unsafe" in violation for violation in result.violations)
        assert any("organization_id" in violation for violation in result.violations)
    finally:
        metadata.remove(unsafe)


async def test_live_guard_flags_table_without_rls(tenant_database: Any) -> None:
    from nexus_ai.infrastructure.orm import TENANT_SELF

    probe = Table(
        "guard_probe_no_rls",
        metadata,
        Column("id", String(36), primary_key=True),
        info={TENANT_SCOPED_KEY: TENANT_SELF},
    )
    try:
        async with tenant_database.engine.connect() as connection:
            result = await check_live(connection)
        assert not result.ok
        assert any("guard_probe_no_rls" in v for v in result.violations)
    finally:
        metadata.remove(probe)


async def test_migration_role_owns_table_runtime_role_does_not(tenant_database: Any) -> None:
    async with tenant_database.session() as session:
        owner = (
            await session.execute(
                text("SELECT tableowner FROM pg_tables WHERE tablename = 'organizations'")
            )
        ).scalar_one()
    assert owner == "nexus_migration"
