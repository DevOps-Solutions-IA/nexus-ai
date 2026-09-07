"""Operational scripts: schema guard CLI and DB role bootstrap (sections 25, 28, 93)."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


def test_schema_guard_static_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.nxs_schema_guard.__main__ import main

    monkeypatch.setattr("sys.argv", ["nxs_schema_guard", "--static-only"])
    assert main() == 0


def test_schema_guard_live_cli(migrated_database: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.nxs_schema_guard.__main__ import main
    from tests.conftest import MIGRATION_DSN

    monkeypatch.setenv("NXS_DATABASE__MIGRATION_DSN", MIGRATION_DSN)
    monkeypatch.setenv("NXS_ENVIRONMENT", "test")
    monkeypatch.setattr("sys.argv", ["nxs_schema_guard"])
    assert main() == 0


def test_db_bootstrap_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.nxs_dbadmin.__main__ import main
    from tests.conftest import SUPERUSER_DSN

    monkeypatch.setattr("sys.argv", ["nxs_dbadmin", "bootstrap", "--superuser-dsn", SUPERUSER_DSN])
    assert main() == 0
    assert main() == 0  # second run must also succeed
