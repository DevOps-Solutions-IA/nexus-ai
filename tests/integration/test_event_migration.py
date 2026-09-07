"""Event platform migration: reversibility, RLS classification and grants (NXS-DATA-002)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

_TENANT_TABLES = ("event_outbox", "event_dead_letters")


async def test_tenant_event_tables_have_forced_rls_and_a_relay_policy(
    tenant_database: Any,
) -> None:
    async with tenant_database.engine.connect() as connection:
        for table in _TENANT_TABLES:
            row = (
                await connection.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname = :name"
                    ),
                    {"name": table},
                )
            ).one()
            assert row[0] is True and row[1] is True, table
            policies = (
                (
                    await connection.execute(
                        text("SELECT policyname FROM pg_policies WHERE tablename = :name"),
                        {"name": table},
                    )
                )
                .scalars()
                .all()
            )
            assert "nxs_tenant_isolation" in policies
            assert "nxs_system_relay" in policies


async def test_consumer_receipts_is_platform_internal_without_rls(tenant_database: Any) -> None:
    async with tenant_database.engine.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT relrowsecurity FROM pg_class WHERE relname = 'consumer_receipts'")
            )
        ).one()
    assert row[0] is False


async def test_runtime_role_grants_exclude_delete(tenant_database: Any) -> None:
    async with tenant_database.engine.connect() as connection:
        for table in ("event_outbox", "event_dead_letters", "consumer_receipts"):
            grants = (
                (
                    await connection.execute(
                        text(
                            "SELECT privilege_type FROM information_schema.role_table_grants "
                            "WHERE grantee = 'nexus_runtime' AND table_name = :name"
                        ),
                        {"name": table},
                    )
                )
                .scalars()
                .all()
            )
            assert set(grants) == {"SELECT", "INSERT", "UPDATE"}, (table, grants)


async def test_tables_owned_by_migration_role(tenant_database: Any) -> None:
    async with tenant_database.engine.connect() as connection:
        owners = (
            await connection.execute(
                text(
                    "SELECT tablename, tableowner FROM pg_tables "
                    "WHERE tablename IN ('event_outbox', 'event_dead_letters', 'consumer_receipts')"
                )
            )
        ).all()
    assert {owner for _, owner in owners} == {"nexus_migration"}
