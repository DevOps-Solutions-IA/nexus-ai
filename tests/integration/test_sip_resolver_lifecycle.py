"""Actual internal application startup verifies both database roles and shuts down."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.engine import make_url

import nexus_ai.sip_edge.runtime as runtime
from nexus_ai.core.config import DatabaseSettings, Settings
from nexus_ai.core.errors import ConfigurationError
from tests.conftest import RUNTIME_DSN
from tests.unit.test_sip_runtime_configuration import configuration

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.mark.parametrize("wrong_role", [False, True])
async def test_resolver_real_role_startup_health_and_shutdown(
    tenant_database: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wrong_role: bool
) -> None:
    secret = tmp_path / "resolver.json"
    secret.write_text(json.dumps(configuration()))
    secret.chmod(0o600)
    locator = make_url(RUNTIME_DSN).set(
        username="nexus_sip_locator", password="local-sip-locator-only"
    )
    settings = Settings(
        database=DatabaseSettings(dsn=SecretStr(RUNTIME_DSN)),
        sip_edge={
            "enabled": True,
            "secret_file": str(secret),
            "locator_dsn": RUNTIME_DSN
            if wrong_role
            else locator.render_as_string(hide_password=False),
        },
    )
    monkeypatch.setattr(runtime, "Settings", lambda: settings)
    app = runtime.create_application()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://resolver"
    ) as client:
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/health/ready")).status_code == 503
        if wrong_role:
            with pytest.raises(ConfigurationError, match="roles are unsafe"):
                async with app.router.lifespan_context(app):
                    pytest.fail("unsafe locator role started")
        else:
            async with app.router.lifespan_context(app):
                assert (await client.get("/health/ready")).json() == {"status": "READY"}
                assert (await client.post("/internal/sip/inbound", json={})).status_code == 403
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/health/ready")).status_code == 503
