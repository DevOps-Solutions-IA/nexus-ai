"""Strict platform configuration, vocabulary and separate worker lifecycle."""

import asyncio
import datetime as dt
import signal
from unittest.mock import AsyncMock, Mock
from uuid import uuid7

import pytest
from pydantic import SecretStr, ValidationError

from nexus_ai.audit.platform import __main__ as cli
from nexus_ai.audit.platform.config import PlatformAuditSettings
from nexus_ai.audit.platform.contracts import PlatformAuditActor, PlatformAuditIntent


def settings():
    return PlatformAuditSettings(
        database_dsn=SecretStr("postgresql://nexus_audit_platform:local@localhost/nexus_local")
    )


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://nexus_runtime:local@localhost/nexus_local",
        "postgresql://nexus_audit_platform:local@localhost/nexus_local?options=bad",
        "https://nexus_audit_platform@localhost/nexus_local",
    ],
)
def test_dedicated_connection_configuration(dsn):
    with pytest.raises(ValidationError):
        PlatformAuditSettings(database_dsn=SecretStr(dsn))


@pytest.mark.parametrize(
    "change",
    [
        {"scope": "TENANT"},
        {"organization_id": uuid7()},
        {"version": 2},
        {"action": "DROP TABLE users"},
        {"metadata": {"token": "secret"}},
        {"metadata": {"enabled": "true"}},
        {"occurred_at": dt.datetime(2026, 1, 1)},
        {"actor": {"kind": "SERVICE", "service": "model"}},
        {"actor": {"kind": "HUMAN"}},
        {"actor": {"kind": "SERVICE", "service": "sentinel-store", "user_id": uuid7()}},
    ],
)
def test_closed_platform_intent(change):
    with pytest.raises(ValidationError):
        PlatformAuditIntent.model_validate(
            {
                "source_id": uuid7(),
                "action": "sentinel.proposal.recorded",
                "target_type": "proposal",
                "target_id": uuid7(),
                "actor": {"kind": "SERVICE", "service": "sentinel-store"},
                "occurred_at": dt.datetime.now(dt.UTC),
                **change,
            }
        )


def test_human_provenance_has_no_login_organization():
    actor = PlatformAuditActor(kind="HUMAN", user_id=uuid7())
    assert set(actor.model_dump()) == {"kind", "user_id", "service"}


@pytest.mark.anyio
async def test_cli_once_closes_connection(monkeypatch):
    database = Mock(close=AsyncMock())
    worker = Mock(run_once=AsyncMock(return_value=0))
    monkeypatch.setattr(cli, "PlatformAuditDatabase", lambda _: database)
    monkeypatch.setattr(cli, "PlatformAuditWorker", lambda _: worker)
    await cli.run(settings(), once=True)
    worker.run_once.assert_awaited_once()
    database.close.assert_awaited_once()


@pytest.mark.anyio
async def test_cli_shutdown_cancels_inflight_transaction(monkeypatch):
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    callbacks = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "add_signal_handler", lambda signum, callback: callbacks.update({signum: callback})
    )
    monkeypatch.setattr(loop, "remove_signal_handler", lambda _: True)

    async def processing():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    database = Mock(close=AsyncMock())
    monkeypatch.setattr(cli, "PlatformAuditDatabase", lambda _: database)
    monkeypatch.setattr(cli, "PlatformAuditWorker", lambda _: Mock(run_once=processing))
    running = asyncio.create_task(cli.run(settings()))
    await entered.wait()
    callbacks[signal.SIGTERM]()
    await asyncio.wait_for(running, timeout=1)
    assert cancelled.is_set()
    database.close.assert_awaited_once()


def test_cli_missing_configuration_and_sanitized_failure(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["platform-audit", "--once"])
    monkeypatch.delenv("NXS_AUDIT_PLATFORM_DSN", raising=False)
    with pytest.raises(SystemExit):
        cli.main()
    monkeypatch.setenv("NXS_AUDIT_PLATFORM_DSN", "private-secret")
    assert cli.main() == 1
    assert "private-secret" not in capsys.readouterr().out
