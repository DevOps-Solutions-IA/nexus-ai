"""Migration/runtime database role separation and reversibility (sections 15, 60)."""

from __future__ import annotations

import pytest

from tests.conftest import MIGRATION_DSN, RUNTIME_DSN

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


def _pg(dsn: str) -> str:
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


async def test_runtime_role_cannot_create_tables(migrated_database: str) -> None:
    import asyncpg

    connection = await asyncpg.connect(_pg(RUNTIME_DSN), timeout=10)
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute("CREATE TABLE runtime_should_not_create (id int)")
    finally:
        await connection.close()


async def test_migration_role_can_manage_schema(migrated_database: str) -> None:
    import asyncpg

    connection = await asyncpg.connect(_pg(MIGRATION_DSN), timeout=10)
    try:
        await connection.execute("CREATE TABLE migration_probe (id int)")
        await connection.execute("DROP TABLE migration_probe")
    finally:
        await connection.close()


async def test_downgrade_then_upgrade_is_clean(migrated_database: str) -> None:
    import anyio
    from alembic import command
    from alembic.config import Config

    from tests.conftest import REPO_ROOT

    config = Config(str(REPO_ROOT / "alembic.ini"))

    def _cycle() -> None:
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        command.check(config)

    await anyio.to_thread.run_sync(_cycle)
