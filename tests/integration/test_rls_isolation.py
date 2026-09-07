"""Row-Level Security attack matrix against real PostgreSQL (sections 53, 54, 77).

These tests use raw SQL deliberately — they must not rely on repository methods for the
security assertions.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from nexus_ai.infrastructure.schema_guard import check_live

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

_SELECT_ALL = text("SELECT organization_key FROM organizations")


async def test_same_tenant_select_insert_update(tenant_database: Any, make_organization) -> None:
    org = await make_organization()
    async with tenant_database.tenant_transaction(org.id) as ts:
        keys = (await ts.session.execute(_SELECT_ALL)).scalars().all()
        assert keys == [org.organization_key]
        await ts.session.execute(text("UPDATE organizations SET display_name = 'Renamed'"))
        renamed = (
            await ts.session.execute(text("SELECT display_name FROM organizations"))
        ).scalar_one()
        assert renamed == "Renamed"


async def test_cross_tenant_select_blocked(tenant_database: Any, make_organization) -> None:
    a = await make_organization()
    b = await make_organization()
    async with tenant_database.tenant_transaction(a.id) as ts:
        rows = (await ts.session.execute(_SELECT_ALL)).scalars().all()
        assert rows == [a.organization_key]
        assert b.organization_key not in rows
        by_id = (
            await ts.session.execute(
                text("SELECT count(*) FROM organizations WHERE id = :bid"), {"bid": str(b.id)}
            )
        ).scalar_one()
        assert by_id == 0


async def test_cross_tenant_update_blocked(tenant_database: Any, make_organization) -> None:
    a = await make_organization()
    b = await make_organization()
    async with tenant_database.tenant_transaction(a.id) as ts:
        result = await ts.session.execute(
            text("UPDATE organizations SET display_name = 'HACKED' WHERE id = :bid"),
            {"bid": str(b.id)},
        )
        assert result.rowcount == 0
    async with tenant_database.tenant_transaction(b.id) as ts:
        name = (
            await ts.session.execute(text("SELECT display_name FROM organizations"))
        ).scalar_one()
        assert name != "HACKED"


async def test_cross_tenant_insert_blocked(tenant_database: Any, make_organization) -> None:
    a = await make_organization()
    other_id = uuid.uuid7()
    async with tenant_database.tenant_transaction(a.id) as ts:
        with pytest.raises(DBAPIError):
            await ts.session.execute(
                text(
                    "INSERT INTO organizations "
                    "(id, organization_key, display_name, legal_name, country_code, timezone, "
                    " status, version) "
                    "VALUES (:id, 'intruder', 'X', 'X', 'US', 'UTC', 'ACTIVE', 1)"
                ),
                {"id": str(other_id)},
            )


async def test_cross_tenant_delete_unavailable(
    raw_runtime_connection: Any, make_organization
) -> None:
    org = await make_organization()
    with pytest.raises(Exception):  # noqa: B017 - asyncpg InsufficientPrivilegeError
        await raw_runtime_connection.execute("DELETE FROM organizations")
    _ = org


async def test_no_scope_sees_zero_rows(tenant_database: Any, make_organization) -> None:
    await make_organization()
    await make_organization()
    async with tenant_database.session() as session:
        rows = (await session.execute(_SELECT_ALL)).scalars().all()
        assert rows == []
    async with tenant_database.transaction() as session:
        rows = (await session.execute(_SELECT_ALL)).scalars().all()
        assert rows == []


async def test_no_scope_insert_blocked(tenant_database: Any) -> None:
    async with tenant_database.transaction() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "INSERT INTO organizations "
                    "(id, organization_key, display_name, legal_name, country_code, timezone, "
                    " status, version) "
                    "VALUES (gen_random_uuid(), 'noscope', 'X', 'X', 'US', 'UTC', 'ACTIVE', 1)"
                )
            )


async def test_force_rls_and_policies_present(tenant_database: Any) -> None:
    async with tenant_database.engine.connect() as connection:
        result = await check_live(connection)
    assert result.ok, result.violations
    assert "organizations" in result.tables_checked


async def test_context_is_transaction_local(tenant_database: Any, make_organization) -> None:
    org = await make_organization()
    async with tenant_database.tenant_transaction(org.id):
        pass
    # After the tenant transaction closes, a plain session on a (possibly reused) connection
    # must not inherit the scope.
    async with tenant_database.session() as session:
        setting = (
            await session.execute(text("SELECT current_setting('nxs.organization_id', true)"))
        ).scalar_one_or_none()
        assert setting in (None, "")


async def test_synthetic_scale_no_collisions(organization_service: Any) -> None:
    from nexus_ai.domain.organizations.entities import OrganizationDraft

    keys: set[str] = set()
    ids: set[uuid.UUID] = set()
    run = uuid.uuid4().hex[:8]
    for index in range(60):
        draft = OrganizationDraft(
            organization_key=f"scale-org-{run}-{index:04d}",
            display_name=f"Scale {index}",
            legal_name=f"Scale {index} SA",
            country_code="us",
            timezone="UTC",
        )
        org = await organization_service.create_core_record(draft)
        keys.add(org.organization_key)
        ids.add(org.id)
    assert len(keys) == 60
    assert len(ids) == 60
