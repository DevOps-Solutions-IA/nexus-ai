"""Sentinel migration proof in disposable databases, never production."""

import asyncio
import os
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from tests.conftest import MIGRATION_DSN, RUNTIME_DSN, SUPERUSER_DSN

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.mark.parametrize("from_canonical", [False, True])
async def test_sentinel_upgrade_downgrade_reupgrade(migrated_database, from_canonical):
    name = f"nxs_p20_test_{uuid4().hex}"
    control = await asyncpg.connect(SUPERUSER_DSN)
    await control.execute(f'CREATE DATABASE "{name}" OWNER nexus_migration')
    env = dict(os.environ)
    env["NXS_ENVIRONMENT"] = "test"
    env["NXS_DATABASE__MIGRATION_DSN"] = (
        make_url(MIGRATION_DSN).set(database=name).render_as_string(hide_password=False)
    )
    env["NXS_DATABASE__DSN"] = (
        make_url(RUNTIME_DSN).set(database=name).render_as_string(hide_password=False)
    )

    async def run(*arguments):
        process = await asyncio.create_subprocess_exec(
            "uv",
            "run",
            *arguments,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        async with asyncio.timeout(120):
            output, _ = await process.communicate()
        assert process.returncode == 0, output.decode()[-6000:]
        return output.decode()

    try:
        if from_canonical:
            await run("alembic", "upgrade", "d19d1b1c2d3e")
        await run("alembic", "upgrade", "head")
        await run("alembic", "check")
        assert (await run("alembic", "heads")).count("(head)") == 1
        assert "d20a0b1c2d3e" in await run("alembic", "history")
        await run("python", "-m", "scripts.nxs_schema_guard")
        connection = await asyncpg.connect(
            make_url(SUPERUSER_DSN)
            .set(database=name, username="nexus_sentinel", password="local-sentinel-only")
            .render_as_string(hide_password=False)
        )
        try:
            assert await connection.fetchval("SELECT current_user") == "nexus_sentinel"
            assert await connection.fetchval("SELECT revision FROM sentinel_control_state") == 1
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.fetch("SELECT * FROM organizations")
        finally:
            await connection.close()
        await run("alembic", "downgrade", "d19d1b1c2d3e")
        await run("alembic", "upgrade", "head")
        await run("alembic", "check")
        await run("python", "-m", "scripts.nxs_schema_guard")
    finally:
        await control.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await control.close()
