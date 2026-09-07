"""The runtime role cannot bypass tenancy (NXS-SEC-003, sections 23, 24, 55, 60)."""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_runtime_role_attributes(tenant_database: Any) -> None:
    report = await tenant_database.runtime_role_report()
    assert report.role == "nexus_runtime"
    assert report.is_superuser is False
    assert report.can_bypass_rls is False
    assert report.can_create_role is False
    assert report.can_create_db is False
    assert report.can_bypass_tenancy is False


async def test_runtime_role_cannot_disable_row_security(
    raw_runtime_connection: Any, make_organization
) -> None:
    import asyncpg

    a = await make_organization()
    b = await make_organization()
    await raw_runtime_connection.execute("SET row_security = off")
    await raw_runtime_connection.execute(
        "SELECT set_config('nxs.organization_id', $1, false)", str(a.id)
    )
    # PostgreSQL refuses a query that would be affected by a policy the role cannot bypass;
    # either way, B is never visible.
    try:
        rows = await raw_runtime_connection.fetch("SELECT organization_key FROM organizations")
    except asyncpg.InsufficientPrivilegeError:
        return
    keys = {row["organization_key"] for row in rows}
    assert b.organization_key not in keys


async def test_runtime_role_cannot_alter_or_drop_policy(raw_runtime_connection: Any) -> None:
    for statement in (
        "ALTER TABLE organizations DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE organizations NO FORCE ROW LEVEL SECURITY",
        "DROP POLICY nxs_tenant_isolation ON organizations",
        "ALTER TABLE organizations ADD COLUMN sneaky text",
        "DROP TABLE organizations",
    ):
        with pytest.raises(Exception):  # noqa: B017 - asyncpg InsufficientPrivilege / others
            await raw_runtime_connection.execute(statement)


async def test_runtime_role_cannot_delete(raw_runtime_connection: Any, make_organization) -> None:
    await make_organization()
    with pytest.raises(Exception):  # noqa: B017 - InsufficientPrivilegeError
        await raw_runtime_connection.execute(
            "SELECT set_config('nxs.organization_id', "
            "(SELECT id::text FROM organizations LIMIT 1), false)"
        )
        await raw_runtime_connection.execute("DELETE FROM organizations")


async def test_runtime_role_cannot_create_roles(raw_runtime_connection: Any) -> None:
    with pytest.raises(Exception):  # noqa: B017 - InsufficientPrivilegeError
        await raw_runtime_connection.execute("CREATE ROLE evil LOGIN")


async def test_startup_fails_closed_when_role_can_bypass_rls(
    migrated_database: str, integration_env
) -> None:
    from nexus_ai.core.errors import ConfigurationError
    from nexus_ai.core.lifecycle import ApplicationLifespan

    # Point the runtime DSN at the superuser role and require verification: startup must refuse.
    settings = integration_env(
        NXS_ENVIRONMENT="staging",
        NXS_DATABASE__DSN=(
            "postgresql+asyncpg://nexus_local:local-development-only@127.0.0.1:15432/nexus_local"
        ),
        NXS_CACHE__REQUIRED="false",
        NXS_MESSAGING__REQUIRED="false",
        NXS_TENANCY__HEADER_RESOLVER_ENABLED="false",
        NXS_TELEMETRY__MODE="local",
        NXS_HTTP__ALLOWED_HOSTS='["staging.nexus-ai.dev"]',
    )
    lifespan = ApplicationLifespan(settings)
    with pytest.raises(ConfigurationError, match="bypass tenant RLS"):
        await lifespan.startup()
    await lifespan.shutdown()
