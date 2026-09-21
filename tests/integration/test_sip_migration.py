"""P19 schema round-trip uses only disposable databases and non-bypass roles."""

import asyncio
import os
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from tests.conftest import MIGRATION_DSN, RUNTIME_DSN, SUPERUSER_DSN

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.mark.parametrize("from_p18", [False, True])
async def test_p19_fresh_and_p18_upgrade_roundtrip(from_p18: bool) -> None:
    name = "nxs_p19_migration_" + uuid4().hex
    control = await asyncpg.connect(SUPERUSER_DSN)
    await control.execute(f'CREATE DATABASE "{name}" OWNER nexus_migration')
    environment = dict(os.environ)
    environment["NXS_ENVIRONMENT"] = "test"
    environment["NXS_DATABASE__DSN"] = (
        make_url(RUNTIME_DSN).set(database=name).render_as_string(hide_password=False)
    )
    environment["NXS_DATABASE__MIGRATION_DSN"] = (
        make_url(MIGRATION_DSN).set(database=name).render_as_string(hide_password=False)
    )

    async def run(*arguments: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "uv",
            "run",
            *arguments,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        async with asyncio.timeout(120):
            output, _ = await process.communicate()
        assert process.returncode == 0, output.decode()[-4000:]
        return output.decode()

    try:
        if from_p18:
            await run("alembic", "upgrade", "c18a0b1c2d3e")
        await run("alembic", "upgrade", "head")
        await run("alembic", "check")
        assert (await run("alembic", "heads")).count("(head)") == 1
        await run("python", "-m", "scripts.nxs_schema_guard")
        connection = await asyncpg.connect(
            make_url(environment["NXS_DATABASE__DSN"])
            .set(drivername="postgresql")
            .render_as_string(hide_password=False)
        )
        try:
            tables = await connection.fetch(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = ANY($1::text[])",
                [
                    "sip_route_authorizations",
                    "sip_route_history",
                    "sip_egress_permits",
                    "sip_egress_routes",
                    "sip_egress_history",
                    "sip_dialog_bindings",
                    "sip_egress_dialog_bindings",
                    "sip_did_locators",
                    "sip_account_upstreams",
                    "sip_account_upstream_history",
                    "sip_call_admissions",
                ],
            )
            assert len(tables) == 11 and all(row[1] and row[2] for row in tables)
            roles = await connection.fetch(
                "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles "
                "WHERE rolname IN ('nexus_runtime','nexus_sip_locator')"
            )
            assert len(roles) == 2 and all(not row[1] and not row[2] for row in roles)
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM pg_proc WHERE proname LIKE 'nxs_sip_%' AND prosecdef"
                )
                == 0
            )
            assert await connection.fetchval("SELECT count(*) FROM sip_did_locators") == 0
            assert await connection.fetchval("SELECT count(*) FROM cell_sip_targets") == 0
        finally:
            await connection.close()
        await run("alembic", "downgrade", "c18a0b1c2d3e")
        await run("alembic", "upgrade", "head")
        await run("alembic", "check")
        await run("python", "-m", "scripts.nxs_schema_guard")
    finally:
        await control.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await control.close()
