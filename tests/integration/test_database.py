"""Real PostgreSQL connectivity, transactions and health (NXS-DATA-001, sections 24, 43, 48)."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from nexus_ai.core.config import Settings
from nexus_ai.core.health import HealthStatus
from nexus_ai.infrastructure.database import Database

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.fixture
async def database(integration_env: Callable[..., Settings]):
    from tests.conftest import MIGRATION_DSN

    # The P01 Database primitive is exercised here as a SYSTEM session (schema DDL),
    # so it connects with the migration role rather than the non-DDL runtime role.
    settings = integration_env(NXS_DATABASE__DSN=MIGRATION_DSN, NXS_DATABASE__POOL_SIZE="1")
    db = Database(settings.database)
    await db.connect()
    try:
        yield db
    finally:
        await db.disconnect()


async def test_connect_and_query(database: Database) -> None:
    async with database.session() as session:
        result = await session.execute(text("SELECT 1"))
        assert result.scalar_one() == 1


async def test_probe_reports_up(database: Database) -> None:
    health = await database.probe(timeout=5)
    assert health.status is HealthStatus.UP
    assert health.latency_ms is not None
    assert health.name == "postgresql"


async def test_transaction_commits_and_is_visible_afterwards(database: Database) -> None:
    create = text("CREATE TABLE _nxs_it_tx (v int)")
    insert = text("INSERT INTO _nxs_it_tx VALUES (42)")
    select = text("SELECT v FROM _nxs_it_tx")
    drop = text("DROP TABLE IF EXISTS _nxs_it_tx")
    try:
        async with database.transaction() as session:
            await session.execute(create)
            await session.execute(insert)
        async with database.transaction() as session:
            assert (await session.execute(select)).scalar_one() == 42
    finally:
        async with database.transaction() as session:
            await session.execute(drop)


async def test_transaction_rolls_back_on_error(database: Database) -> None:
    create = text("CREATE TABLE _nxs_it_rb (v int)")
    select = text("SELECT * FROM _nxs_it_rb")
    with pytest.raises(RuntimeError):
        async with database.transaction() as session:
            await session.execute(create)
            raise RuntimeError("boom")
    raised = False
    try:
        async with database.session() as session:
            await session.execute(select)
    except DBAPIError:
        raised = True
    assert raised, "rolled-back table must not exist"


async def test_disconnect_is_idempotent(database: Database) -> None:
    await database.disconnect()
    await database.disconnect()
    assert not database.is_connected


async def test_unreachable_database_probe_is_down(
    integration_env: Callable[..., Settings],
) -> None:
    settings: Settings = integration_env(
        NXS_DATABASE__DSN="postgresql+asyncpg://x:y@127.0.0.1:5999/none",
        NXS_DATABASE__CONNECT_TIMEOUT_SECONDS="2",
    )
    db = Database(settings.database)
    await db.connect()
    try:
        health = await db.probe(timeout=3)
        assert health.status is HealthStatus.DOWN
    finally:
        await db.disconnect()
