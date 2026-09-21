"""P18 upgrades and rollback use isolated, uniquely named disposable databases."""

import asyncio
import os
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from tests.conftest import MIGRATION_DSN, RUNTIME_DSN, SUPERUSER_DSN

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.mark.parametrize("from_canonical", [False, True])
async def test_disposable_upgrade_downgrade_reupgrade(
    migrated_database: str, from_canonical: bool
) -> None:
    name = f"nxs_p18_test_{uuid4().hex}"
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

    async def run(*arguments: str) -> str:
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
            await run("alembic", "upgrade", "f17a0b1c2d3e")
        await run("alembic", "upgrade", "head")
        await run("alembic", "check")
        assert (await run("alembic", "heads")).count("(head)") == 1
        assert "c18a0b1c2d3e" in await run("alembic", "history")
        await run("python", "-m", "scripts.nxs_schema_guard")
        database: Any = await asyncpg.connect(
            make_url(env["NXS_DATABASE__DSN"])
            .set(drivername="postgresql")
            .render_as_string(hide_password=False)
        )
        try:
            rows = await database.fetch(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname IN ('organization_placements','placement_mutations')"
            )
            assert len(rows) == 2 and all(row[1] and row[2] for row in rows)
            assert await database.fetchval("SELECT count(*) FROM organization_placements") == 0
            assert await database.fetchval("SELECT count(*) FROM cells") == 0
            assert await database.fetchval("SELECT count(*) FROM cell_control_history") == 0
        finally:
            await database.close()
        await run("alembic", "downgrade", "f17a0b1c2d3e")
        await run("alembic", "upgrade", "head")
        await run("alembic", "check")
        await run("python", "-m", "scripts.nxs_schema_guard")
    finally:
        await control.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await control.close()
