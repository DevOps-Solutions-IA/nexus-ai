from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import HTTPException
from pydantic import BaseModel

from nexus_ai.api.dependencies import RequestMetadataDep
from nexus_ai.application import create_app
from nexus_ai.core.config import Settings, get_settings
from nexus_ai.core.errors import NotFoundError

REPO_ROOT = Path(__file__).parents[1]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


_TEST_ENV = {
    "NXS_ENVIRONMENT": "test",
    "NXS_DATABASE__REQUIRED": "false",
    "NXS_CACHE__REQUIRED": "false",
    "NXS_MESSAGING__REQUIRED": "false",
    "NXS_LOGGING__FORMAT": "json",
    "NXS_AUTH__ALLOW_EPHEMERAL_SIGNING_KEY": "true",
    "NXS_AUTH__RATE_LIMIT_BACKEND": "local",
    # A fixed, deterministic OTP pepper for the whole test run (a real >= 32-char
    # secret, valid in every environment — the hardened startup check is satisfied).
    "NXS_OTP__PEPPER": "test-otp-pepper-0123456789abcdef0123456789abcdef",
}


@pytest.fixture(autouse=True)
def _clean_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def test_env(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    def _apply(**overrides: str) -> None:
        for key in list(os.environ):
            if key.startswith("NXS_"):
                monkeypatch.delenv(key, raising=False)
        for key, value in {**_TEST_ENV, **overrides}.items():
            monkeypatch.setenv(key, value)

    return _apply


@pytest.fixture
def build_settings(test_env: Callable[..., None]) -> Callable[..., Settings]:
    def _build(**overrides: str) -> Settings:
        test_env(**overrides)
        return Settings()

    return _build


@pytest.fixture
async def app_client(
    build_settings: Callable[..., Settings],
) -> AsyncIterator[httpx.AsyncClient]:
    settings = build_settings()
    app = create_app(settings)
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://nexus.test") as client:
            yield client


@pytest.fixture
def make_client(build_settings: Callable[..., Settings]):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _make(
        *, routes: Callable[[Any], None] | None = None, **overrides: str
    ) -> AsyncIterator[httpx.AsyncClient]:
        settings = build_settings(**overrides)
        app = create_app(settings)
        if routes is not None:
            routes(app)
        async with LifespanManager(app) as manager:
            transport = httpx.ASGITransport(app=manager.app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://nexus.test"
            ) as client:
                client.nexus_app = app  # type: ignore[attr-defined]
                client.nexus_settings = settings  # type: ignore[attr-defined]
                yield client

    return _make


_PG_HOST = os.environ.get("NXS_IT_PG_HOSTPORT", "127.0.0.1:15432")
RUNTIME_DSN = os.environ.get(
    "NXS_IT_RUNTIME_DSN",
    f"postgresql+asyncpg://nexus_runtime:local-runtime-only@{_PG_HOST}/nexus_local",
)
MIGRATION_DSN = os.environ.get(
    "NXS_IT_MIGRATION_DSN",
    f"postgresql+asyncpg://nexus_migration:local-migration-only@{_PG_HOST}/nexus_local",
)
SUPERUSER_DSN = os.environ.get(
    "NXS_IT_SUPERUSER_DSN",
    f"postgresql://nexus_local:local-development-only@{_PG_HOST}/nexus_local",
)

_IT_DEFAULTS = {
    "NXS_ENVIRONMENT": "test",
    "NXS_LOGGING__FORMAT": "json",
    "NXS_DATABASE__REQUIRED": "true",
    "NXS_CACHE__REQUIRED": "true",
    "NXS_MESSAGING__REQUIRED": "true",
    "NXS_DATABASE__DSN": RUNTIME_DSN,
    "NXS_DATABASE__MIGRATION_DSN": MIGRATION_DSN,
    "NXS_CACHE__URL": os.environ.get("NXS_IT_CACHE_URL", "redis://127.0.0.1:16379/0"),
    "NXS_MESSAGING__URL": os.environ.get("NXS_IT_MESSAGING_URL", "nats://127.0.0.1:14222"),
    "NXS_HEALTH__CACHE_TTL_SECONDS": "0",
    "NXS_TENANCY__HEADER_RESOLVER_ENABLED": "true",
}


@pytest.fixture
def integration_env(test_env: Callable[..., None]) -> Callable[..., Settings]:
    def _build(**overrides: str) -> Settings:
        test_env(**{**_IT_DEFAULTS, **overrides})
        return Settings()

    return _build


@pytest.fixture
async def integration_client(
    integration_env: Callable[..., Settings],
) -> AsyncIterator[httpx.AsyncClient]:
    settings = integration_env()
    app = create_app(settings)
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://nexus.test") as client:
            client.nexus_app = app  # type: ignore[attr-defined]
            yield client


@pytest.fixture
def add_boom_route() -> Callable[[Any], None]:
    def _add(app: Any) -> None:
        @app.get("/_diagnostics/boom")
        async def _boom() -> None:  # pragma: no cover - body never returns
            raise RuntimeError("connect failed postgresql://u:hunter2@10.0.0.9:5432/nx")

    return _add


class DiagBody(BaseModel):
    name: str
    count: int


async def _diag_echo(body: DiagBody) -> dict[str, object]:
    return {"name": body.name, "count": body.count}


async def _diag_nxs_error() -> None:
    raise NotFoundError("The requested diagnostic resource is absent.")


async def _diag_teapot() -> None:
    raise HTTPException(status_code=418, detail="I am a teapot")


async def _diag_meta(meta: RequestMetadataDep) -> dict[str, object]:
    return meta.model_dump()


@pytest.fixture
def add_diag_routes() -> Callable[[Any], None]:
    def _add(app: Any) -> None:
        app.post("/_diagnostics/echo")(_diag_echo)
        app.get("/_diagnostics/nxs-error")(_diag_nxs_error)
        app.get("/_diagnostics/teapot")(_diag_teapot)
        app.get("/_diagnostics/meta")(_diag_meta)

    return _add


_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "nxs-test",
    "GIT_AUTHOR_EMAIL": "nxs-test@example.com",
    "GIT_COMMITTER_NAME": "nxs-test",
    "GIT_COMMITTER_EMAIL": "nxs-test@example.com",
}


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", **_GIT_ENV},
    )
    return completed.stdout.strip()


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


_BASELINE_SHA = "0" * 40


def normalize_nxs_baseline(nxs: Path) -> None:
    """Reset a copied .nxs tree to the deterministic 'P00 READY / P01 PLANNED' baseline.

    The live repository state advances as NXS-P01 is built; tests need a fixed start point.
    """
    registry = _read(nxs / "phase-registry.json")
    for phase in registry["phases"]:
        if phase["id"] == "NXS-P00":
            phase["status"], phase["decision"] = "READY", "GO"
        else:
            phase["status"], phase["decision"] = "PLANNED", "PENDING"
    _write(nxs / "phase-registry.json", registry)

    state = _read(nxs / "project-state.json")
    state["current_phase"] = {
        "id": "NXS-P00",
        "status": "READY",
        "decision": "GO",
        "branch": "feat/nxs-p00-engineering-control-system",
        "implementation_commit": _BASELINE_SHA,
        "closure_commit": None,
    }
    state["active_phase"] = None
    state["completed_phases"] = ["NXS-P00"]
    state["blocked_phases"] = []
    state["next_allowed_execution"] = {"phase": "NXS-P01", "condition": "DEPENDENCIES_READY"}
    _write(nxs / "project-state.json", state)

    _write(
        nxs / "execution-lock.json",
        {
            "schema_version": "1.0.0",
            "state": "RELEASED",
            "phase": None,
            "branch": None,
            "actor": None,
            "started_at": None,
            "expires_at": None,
            "repository_commit": None,
        },
    )

    requirements = _read(nxs / "requirements.json")
    for requirement in requirements["requirements"]:
        if requirement["target_phase"] != "NXS-P00":
            requirement["status"] = "PLANNED"
            requirement["implementation_evidence"] = []
            requirement["validation_evidence"] = []
    _write(nxs / "requirements.json", requirements)

    readiness = _read(nxs / "readiness.json")
    readiness["phases"] = [p for p in readiness["phases"] if p["phase"] == "NXS-P00"]
    if not readiness["phases"]:
        readiness["phases"] = [
            {
                "phase": "NXS-P00",
                "branch": "feat/nxs-p00-engineering-control-system",
                "implementation_commit": _BASELINE_SHA,
                "closure_reference": None,
                "requirements": [],
                "gates": {"tests": "PASS"},
                "evidence": ["evidence.json"],
                "test_count": 1,
                "security_status": "PASS",
                "regression_status": "PASS",
                "decision": "GO",
                "status": "READY",
                "timestamp": "2026-09-06T00:00:00Z",
            }
        ]
    _write(nxs / "readiness.json", readiness)

    for manifest_path in (nxs / "phases").glob("NXS-P*.json"):
        if manifest_path.stem == "NXS-P00":
            continue
        manifest = _read(manifest_path)
        manifest["status"], manifest["decision"] = "PLANNED", "PENDING"
        manifest["implementation_commit"] = None
        manifest["closure_commit"] = None
        manifest["evidence"] = []
        manifest["timestamps"] = {"started_at": None, "closed_at": None}
        _write(manifest_path, manifest)

    for evidence_dir in (nxs / "evidence").glob("NXS-P*"):
        if evidence_dir.name != "NXS-P00":
            shutil.rmtree(evidence_dir)


@pytest.fixture
def lifecycle_repo(tmp_path: Path) -> Iterator[Path]:
    """A throwaway Git repository seeded with the real .nxs control state at P00 READY/GO."""
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copytree(REPO_ROOT / ".nxs", repo / ".nxs")
    normalize_nxs_baseline(repo / ".nxs")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed control state")
    _git(repo, "checkout", "-q", "-b", "feat/nxs-p01-backend-core")
    yield repo


@pytest.fixture
def checkout(lifecycle_repo: Path) -> Callable[[str], None]:
    def _checkout(branch: str) -> None:
        try:
            _git(lifecycle_repo, "checkout", "-q", branch)
        except subprocess.CalledProcessError:
            _git(lifecycle_repo, "checkout", "-q", "-b", branch)

    return _checkout


@pytest.fixture
def seed_evidence(lifecycle_repo: Path) -> Callable[..., None]:
    """Create passing gate + matrix + test evidence for a phase so closure can proceed."""

    def _seed(phase_id: str, *, passed: int = 42) -> None:
        directory = lifecycle_repo / ".nxs/evidence" / phase_id
        directory.mkdir(parents=True, exist_ok=True)
        gates = {"repository_integrity": "PASS", "tests": "PASS", "lint": "PASS"}
        _write(
            directory / "quality-gates.json",
            {"schema_version": "1.0.0", "phase": phase_id, "result": "PASS", "gates": gates},
        )
        _write(
            directory / "quality-matrix.json",
            {"schema_version": "1.0.0", "phase": phase_id, "result": "PASS", "gates": gates},
        )
        _write(directory / "tests.json", {"passed": passed, "failed": 0})

    return _seed


@pytest.fixture
def git_head(lifecycle_repo: Path) -> Callable[[], str]:
    def _head() -> str:
        return _git(lifecycle_repo, "rev-parse", "HEAD")

    return _head


@pytest.fixture
def commit_all(lifecycle_repo: Path) -> Callable[[str], str]:
    def _commit(message: str) -> str:
        _git(lifecycle_repo, "add", "-A")
        _git(lifecycle_repo, "commit", "-q", "-m", message)
        return _git(lifecycle_repo, "rev-parse", "HEAD")

    return _commit


# --- Tenancy integration fixtures (NXS-P02) ---


def _run_async(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)


@pytest.fixture(scope="session")
def migrated_database() -> str:
    """Bootstrap the DB roles and run migrations once per test session (runs as migration role)."""
    from scripts.nxs_dbadmin.__main__ import _bootstrap

    _run_async(_bootstrap(SUPERUSER_DSN))
    from alembic import command
    from alembic.config import Config

    os.environ["NXS_ENVIRONMENT"] = "test"
    os.environ["NXS_DATABASE__DSN"] = RUNTIME_DSN
    os.environ["NXS_DATABASE__MIGRATION_DSN"] = MIGRATION_DSN
    config = Config(str(REPO_ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    return MIGRATION_DSN


@pytest.fixture
async def tenant_database(
    migrated_database: str, integration_env: Callable[..., Settings]
) -> AsyncIterator[Any]:
    from nexus_ai.infrastructure.database import Database

    settings = integration_env()
    database = Database(settings.database, context_setting=settings.tenancy.context_setting_name)
    await database.connect()
    try:
        yield database
    finally:
        await database.disconnect()


@pytest.fixture
async def pool1_database(
    migrated_database: str, integration_env: Callable[..., Settings]
) -> AsyncIterator[Any]:
    from nexus_ai.infrastructure.database import Database

    settings = integration_env(NXS_DATABASE__POOL_SIZE="1", NXS_DATABASE__MAX_OVERFLOW="0")
    database = Database(settings.database, context_setting=settings.tenancy.context_setting_name)
    await database.connect()
    try:
        yield database
    finally:
        await database.disconnect()


@pytest.fixture
async def organization_service(tenant_database: Any) -> Any:
    from nexus_ai.domain.organizations.service import OrganizationService

    return OrganizationService(tenant_database)


@pytest.fixture
async def make_organization(organization_service: Any) -> Callable[..., Any]:
    counter = {"n": 0}

    async def _make(*, activate: bool = True, key: str | None = None) -> Any:
        from nexus_ai.domain.organizations.entities import OrganizationDraft
        from nexus_ai.domain.organizations.status import OrganizationStatus

        counter["n"] += 1
        suffix = f"{counter['n']:04d}-{uuid_hex()}"
        draft = OrganizationDraft(
            organization_key=key or f"synthetic-org-{suffix}",
            display_name=f"Synthetic Org {counter['n']}",
            legal_name=f"Synthetic Org {counter['n']} S.A.",
            country_code="cr",
            timezone="America/Costa_Rica",
        )
        organization = await organization_service.create_core_record(draft)
        if activate:
            organization = await organization_service.transition(
                organization.id, OrganizationStatus.ACTIVE
            )
        return organization

    return _make


def uuid_hex() -> str:
    import uuid

    return uuid.uuid4().hex[:8]


@pytest.fixture
async def raw_runtime_connection() -> AsyncIterator[Any]:
    import asyncpg

    connection = await asyncpg.connect(
        RUNTIME_DSN.replace("postgresql+asyncpg://", "postgresql://", 1), timeout=10
    )
    try:
        yield connection
    finally:
        await connection.close()


@pytest.fixture
async def tenant_header_client(
    migrated_database: str, integration_env: Callable[..., Settings]
) -> AsyncIterator[httpx.AsyncClient]:
    settings = integration_env()
    app = create_app(settings)
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://nexus.test") as client:
            client.nexus_app = app  # type: ignore[attr-defined]
            client.nexus_settings = settings  # type: ignore[attr-defined]
            yield client


@pytest.fixture
async def auth_client(
    migrated_database: str, integration_env: Callable[..., Settings]
) -> AsyncIterator[httpx.AsyncClient]:
    """A full app whose tenant scope resolves ONLY from bearer tokens (NXS-P03)."""
    settings = integration_env(NXS_TENANCY__HEADER_RESOLVER_ENABLED="false")
    app = create_app(settings)
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://nexus.test") as client:
            client.nexus_app = app  # type: ignore[attr-defined]
            client.nexus_settings = settings  # type: ignore[attr-defined]
            yield client


def _auth_resources(client: httpx.AsyncClient) -> Any:
    return client.nexus_app.state.lifespan.resources  # type: ignore[attr-defined]


@pytest.fixture
def make_auth_org(auth_client: httpx.AsyncClient) -> Callable[..., Any]:
    from nexus_ai.domain.organizations.entities import OrganizationDraft
    from nexus_ai.domain.organizations.status import OrganizationStatus

    counter = {"n": 0}

    async def _make(*, activate: bool = True, key: str | None = None) -> Any:
        resources = _auth_resources(auth_client)
        counter["n"] += 1
        draft = OrganizationDraft(
            organization_key=key or f"auth-org-{counter['n']:04d}-{uuid_hex()}",
            display_name=f"Auth Org {counter['n']}",
            legal_name=f"Auth Org {counter['n']} S.A.",
            country_code="cr",
            timezone="America/Costa_Rica",
        )
        organization = await resources.organizations.create_core_record(draft)
        if activate:
            organization = await resources.organizations.transition(
                organization.id, OrganizationStatus.ACTIVE
            )
        return organization

    return _make


@pytest.fixture
def make_auth_user(auth_client: httpx.AsyncClient) -> Callable[..., Any]:
    """Register a user; optionally create an org and attach them with a role."""
    from nexus_ai.domain.auth.entities import RegistrationDraft
    from nexus_ai.domain.auth.rbac import RoleKey

    counter = {"n": 0}

    async def _make(
        *,
        password: str = "correct-horse-battery-staple",  # noqa: S107 - test fixture default
        email: str | None = None,
        organization: Any | None = None,
        role: RoleKey | None = RoleKey.ORG_OWNER,
        email_verified: bool = False,
    ) -> tuple[str, str, Any]:
        resources = _auth_resources(auth_client)
        counter["n"] += 1
        normalized = email or f"user-{counter['n']:04d}-{uuid_hex()}@example.com"
        registered = await resources.auth.register_user(
            RegistrationDraft(
                email=normalized,
                password=password,
                display_name=f"User {counter['n']}",
            ),
            verify_email=email_verified,
        )
        if organization is not None and role is not None:
            await resources.memberships.create(
                actor=None,
                organization_id=organization.id,
                user_id=registered.id,
                role=role,
            )
        return normalized, password, registered

    return _make


@pytest.fixture
def login_helper(auth_client: httpx.AsyncClient) -> Callable[..., Any]:
    async def _login(
        email: str, password: str, organization_id: Any | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"email": email, "password": password}
        if organization_id is not None:
            payload["organization_id"] = str(organization_id)
        response = await auth_client.post("/api/v1/auth/login", json=payload)
        assert response.status_code == 200, response.text
        return response.json()

    return _login


# --- Data and event platform fixtures (NXS-P04) ---


@pytest.fixture
async def nats_messaging(integration_env: Callable[..., Settings]) -> AsyncIterator[Any]:
    from nexus_ai.infrastructure.messaging import Messaging

    settings = integration_env()
    messaging = Messaging(settings.messaging)
    await messaging.connect()
    try:
        yield messaging
    finally:
        await messaging.disconnect()


@pytest.fixture
async def jetstream_reset(nats_messaging: Any) -> Any:
    """Delete the NXS event streams so each test starts from an empty topology."""

    import contextlib

    async def _reset() -> None:
        js = nats_messaging.jetstream()
        for name in ("NXS_EVENTS", "NXS_EVENTS_DLQ"):
            with contextlib.suppress(Exception):
                await js.delete_stream(name)

    await _reset()
    return _reset


@pytest.fixture
async def truncate_event_tables() -> Callable[..., Any]:
    """Truncate the P04 event tables (needs the migration role — runtime has no DELETE)."""
    import asyncpg

    async def _truncate() -> None:
        connection = await asyncpg.connect(
            MIGRATION_DSN.replace("postgresql+asyncpg://", "postgresql://", 1), timeout=10
        )
        try:
            await connection.execute("TRUNCATE event_outbox, event_dead_letters, consumer_receipts")
        finally:
            await connection.close()

    await _truncate()
    return _truncate


@pytest.fixture
async def event_platform(
    tenant_database: Any,
    nats_messaging: Any,
    jetstream_reset: Any,
    truncate_event_tables: Any,
    integration_env: Callable[..., Settings],
) -> AsyncIterator[Any]:
    """An EventPlatform with the background relay OFF so tests drive it deterministically."""
    from nexus_ai.events.service import EventPlatform

    settings = integration_env(
        NXS_EVENTS__PUBLISHER_ENABLED="false",
        NXS_EVENTS__CONSUMERS_ENABLED="false",
        NXS_EVENTS__PUBLISHER_POLL_INTERVAL_SECONDS="0.1",
        NXS_EVENTS__RETRY_BASE_DELAY_SECONDS="0.1",
        NXS_EVENTS__RETRY_MAX_DELAY_SECONDS="1",
        NXS_EVENTS__CONSUMER_ACK_WAIT_SECONDS="2",
        NXS_EVENTS__HANDLER_TIMEOUT_SECONDS="3",
    )
    platform = EventPlatform(settings, tenant_database, nats_messaging, worker_name="test-worker")
    await platform.start()
    try:
        yield platform
    finally:
        await platform.stop()


@pytest.fixture
def make_tenant_event() -> Callable[..., Any]:
    from nexus_ai.events.envelope import EventEnvelope

    def _make(organization_id: Any, *, nonce: str | None = None, **overrides: Any) -> Any:
        import uuid

        params: dict[str, Any] = {
            "event_type": "platform.tenant_probe.emitted",
            "event_version": 1,
            "aggregate_type": "platform",
            "aggregate_id": uuid.uuid4().hex,
            "producer": "nxs-test",
            "payload": {"nonce": nonce or uuid.uuid4().hex},
            "organization_id": organization_id,
        }
        params.update(overrides)
        return EventEnvelope.create(**params)

    return _make


# --- Integration Hub fixtures (NXS-P07) ---


@pytest.fixture
def mock_http_server() -> Iterator[Any]:
    """A controlled local HTTP server (127.0.0.1, ephemeral port) for outbound tests.
    NEVER the public internet."""
    import http.server
    import json as _json
    import threading

    state: dict[str, Any] = {
        "handler": lambda method, path, headers, body: (200, {"ok": True}),
        "requests": [],
    }

    class _ThreadingServer(http.server.ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    class _Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:  # silence
            return

        def _serve(self, method: str) -> None:
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else b""
            state["requests"].append(
                {"method": method, "path": self.path, "headers": dict(self.headers), "body": body}
            )
            result = state["handler"](method, self.path, dict(self.headers), body)
            status, payload = result[0], result[1]
            headers = result[2] if len(result) > 2 else {}
            raw = payload if isinstance(payload, bytes) else _json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", headers.pop("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(raw)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(raw)

        def do_GET(self) -> None:
            self._serve("GET")

        def do_POST(self) -> None:
            self._serve("POST")

        def do_PUT(self) -> None:
            self._serve("PUT")

        def do_PATCH(self) -> None:
            self._serve("PATCH")

        def do_DELETE(self) -> None:
            self._serve("DELETE")

    server = _ThreadingServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    class _Control:
        base_url = f"http://127.0.0.1:{port}"

        @staticmethod
        def set_handler(fn: Callable[..., Any]) -> None:
            state["handler"] = fn

        @property
        def requests(self) -> list[dict[str, Any]]:
            return state["requests"]

    try:
        yield _Control()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
async def integration_hub(
    tenant_database: Any,
    event_platform: Any,
    truncate_event_tables: Any,
    integration_env: Callable[..., Settings],
) -> Any:
    """A fully wired Integration Hub against the real DB + event outbox, with the
    governed executor allowed to reach the local mock server only."""
    import asyncpg
    from cryptography.fernet import Fernet

    from nexus_ai.domain.integrations.repository import (
        IntegrationIdempotencyRepository,
        IntegrationSecretStore,
    )
    from nexus_ai.integrations.auth_profiles import AuthProfileApplier
    from nexus_ai.integrations.circuit import CircuitBreakerRegistry
    from nexus_ai.integrations.credentials import LocalEncryptedVault, build_fernet
    from nexus_ai.integrations.destination import DestinationPolicy
    from nexus_ai.integrations.executor import GovernedHttpExecutor
    from nexus_ai.integrations.ratelimit import OutboundRateLimiter
    from nexus_ai.integrations.registry import IntegrationRegistry
    from nexus_ai.integrations.service import IntegrationHubService
    from nexus_ai.integrations.webhooks import InboundWebhookService

    settings = integration_env()
    policy = DestinationPolicy(allow_loopback=True, resolver=lambda h, p: ["127.0.0.1"])
    vault = LocalEncryptedVault(
        IntegrationSecretStore(tenant_database), build_fernet([Fernet.generate_key().decode()])
    )
    executor = GovernedHttpExecutor(settings.integrations, policy)
    registry = IntegrationRegistry(
        settings, tenant_database, event_platform.publisher, vault, policy
    )
    service = IntegrationHubService(
        settings,
        tenant_database,
        event_platform.publisher,
        registry,
        executor,
        AuthProfileApplier(vault, policy, executor),
        CircuitBreakerRegistry(settings.integrations),
        OutboundRateLimiter(settings.integrations, _NoCache()),
        IntegrationIdempotencyRepository(tenant_database),
    )
    webhooks = InboundWebhookService(settings, tenant_database, event_platform.publisher, vault)

    async def _cleanup() -> None:
        connection = await asyncpg.connect(
            MIGRATION_DSN.replace("postgresql+asyncpg://", "postgresql://", 1), timeout=10
        )
        try:
            await connection.execute(
                "TRUNCATE tool_definitions, tool_execution_records, tool_idempotency_records, "
                "integrations, integration_operations, integration_secrets, "
                "integration_execution_records, integration_idempotency_records, "
                "webhook_endpoints, webhook_receipts CASCADE"
            )
        finally:
            await connection.close()

    await _cleanup()

    class _Hub:
        def __init__(self) -> None:
            self.registry = registry
            self.service = service
            self.webhooks = webhooks
            self.vault = vault
            self.event_platform = event_platform
            self.database = tenant_database
            self.settings = settings

    return _Hub()


@pytest.fixture
async def tool_engine(integration_hub: Any) -> Any:
    """A fully wired Tool Engine on top of the ``integration_hub`` fixture (real DB +
    event outbox + governed Integration Hub reaching the local mock server only)."""
    from nexus_ai.domain.auth.rbac import AuthorizationService
    from nexus_ai.domain.tools.repository import ToolIdempotencyRepository
    from nexus_ai.tools.permissions import ToolPermissionGuard
    from nexus_ai.tools.registry import ToolRegistry
    from nexus_ai.tools.service import ToolEngine

    hub = integration_hub
    registry = ToolRegistry(hub.settings, hub.database, hub.event_platform.publisher, hub.registry)
    engine = ToolEngine(
        hub.settings,
        hub.database,
        hub.event_platform.publisher,
        registry,
        hub.service,
        ToolPermissionGuard(AuthorizationService(hub.database)),
        ToolIdempotencyRepository(hub.database),
    )

    class _ToolEngine:
        def __init__(self) -> None:
            self.registry = registry
            self.engine = engine
            self.hub = hub
            self.database = hub.database
            self.settings = hub.settings
            self.event_platform = hub.event_platform
            self.integrations = hub.registry
            self.mock = None

    return _ToolEngine()


class _NoCache:
    is_connected = False
    client = None


@pytest.fixture
def make_tool_principal(tenant_database: Any) -> Callable[..., Any]:
    """Seed an ACTIVE user + ACTIVE owner membership + org_owner role assignment in an
    Organization and return a :class:`Principal` for it (no HTTP / token round-trip)."""
    import datetime as _dt
    import uuid as _uuid

    from sqlalchemy import text as _text

    from nexus_ai.domain.auth.entities import Principal
    from nexus_ai.domain.auth.rbac import ROLE_IDS, RoleKey

    async def _make(organization: Any, *, role: RoleKey = RoleKey.ORG_OWNER) -> Any:
        user_id = _uuid.uuid7()
        now = _dt.datetime.now(_dt.UTC)
        async with tenant_database.transaction() as session:
            await session.execute(
                _text(
                    "INSERT INTO users (id, email, email_verified, display_name, status, "
                    "version, created_at, updated_at) VALUES (:id, :email, true, 'Tool Tester', "
                    "'ACTIVE', 1, now(), now())"
                ),
                {"id": user_id, "email": f"tool-{user_id.hex[:12]}@example.com"},
            )
        async with tenant_database.tenant_transaction(organization.id) as tenant:
            await tenant.session.execute(
                _text(
                    "INSERT INTO memberships (id, organization_id, user_id, status, "
                    "created_at, updated_at) VALUES (:id, :org, :user, 'ACTIVE', now(), now())"
                ),
                {"id": _uuid.uuid7(), "org": organization.id, "user": user_id},
            )
            await tenant.session.execute(
                _text(
                    "INSERT INTO role_assignments (id, organization_id, user_id, role_id, "
                    "status, created_at, updated_at) VALUES (:id, :org, :user, :role, 'ACTIVE', "
                    "now(), now())"
                ),
                {
                    "id": _uuid.uuid7(),
                    "org": organization.id,
                    "user": user_id,
                    "role": ROLE_IDS[role],
                },
            )
        return Principal(
            user_id=user_id,
            session_id=_uuid.uuid7(),
            organization_id=organization.id,
            token_id=_uuid.uuid7(),
            issued_at=now,
            expires_at=now + _dt.timedelta(hours=1),
        )

    return _make


# --- NXS-P09 messaging channels -------------------------------------------------


class FakeMessagingTransport:
    """An in-memory :class:`MessagingTransport`. Records every request; returns a
    programmed response (or raises a programmed :class:`TransportError`)."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._handler: Callable[..., Any] | None = None
        self._default = (200, {"messages": [{"id": "prov-msg-1"}], "message_id": "prov-msg-1"})

    def set_response(self, status_code: int, body: Any) -> None:
        self._default = (status_code, body)

    def set_handler(self, handler: Callable[..., Any]) -> None:
        self._handler = handler

    async def request(
        self,
        *,
        method: str,
        url: str,
        headers: Any,
        body: bytes | None,
        timeout_seconds: float | None = None,
    ) -> Any:
        import json as _json

        from nexus_ai.messaging.providers.base import TransportError, TransportResponse

        entry = {
            "method": method,
            "url": url,
            "headers": dict(headers),
            "body": body,
            "json": _json.loads(body) if body else None,
        }
        self.requests.append(entry)
        outcome = self._handler(entry) if self._handler is not None else self._default
        if isinstance(outcome, TransportError):
            raise outcome
        status_code, payload = outcome
        raw = payload if isinstance(payload, bytes) else _json.dumps(payload).encode()
        return TransportResponse(status_code=status_code, headers={}, body=raw)


@pytest.fixture
async def messaging_stack(
    tenant_database: Any,
    event_platform: Any,
    truncate_event_tables: Any,
    integration_env: Callable[..., Settings],
) -> Any:
    """A fully wired messaging subsystem against the real DB + event outbox, with a fake
    provider transport (no socket) and the P06 customer / conversation services."""
    import asyncpg
    from cryptography.fernet import Fernet

    from nexus_ai.domain.customers.service import ConversationService, CustomerService
    from nexus_ai.domain.messaging.repository import MessagingSecretStore
    from nexus_ai.integrations.credentials import LocalEncryptedVault, build_fernet
    from nexus_ai.messaging.service import MessagingService
    from nexus_ai.messaging.webhooks import InboundMessagingService

    settings = integration_env()
    vault = LocalEncryptedVault(
        MessagingSecretStore(tenant_database), build_fernet([Fernet.generate_key().decode()])
    )
    transport = FakeMessagingTransport()
    customers = CustomerService(settings, tenant_database, event_platform.publisher)
    conversations = ConversationService(settings, tenant_database, event_platform.publisher)
    service = MessagingService(
        settings,
        tenant_database,
        event_platform.publisher,
        vault,
        transport,
        customers,
        conversations,
    )
    inbound = InboundMessagingService(
        settings,
        tenant_database,
        event_platform.publisher,
        vault,
        customers,
        conversations,
        service,
    )

    async def _cleanup() -> None:
        connection = await asyncpg.connect(
            MIGRATION_DSN.replace("postgresql+asyncpg://", "postgresql://", 1), timeout=10
        )
        try:
            await connection.execute(
                "TRUNCATE otp_challenges, messaging_messages, messaging_send_idempotency, "
                "messaging_inbound_receipts, messaging_secrets, messaging_accounts, "
                "conversation_activities, conversation_participants, conversations, "
                "customer_identities, customers CASCADE"
            )
        finally:
            await connection.close()

    await _cleanup()

    class _MessagingStack:
        def __init__(self) -> None:
            self.service = service
            self.inbound = inbound
            self.transport = transport
            self.vault = vault
            self.customers = customers
            self.conversations = conversations
            self.database = tenant_database
            self.settings = settings
            self.event_platform = event_platform

    return _MessagingStack()


@pytest.fixture
async def otp_stack(messaging_stack: Any) -> Any:
    """The OTP subsystem wired against the real DB + event outbox, delivering through the
    fake-transport messaging stack (no socket)."""
    from nexus_ai.otp.service import OtpService

    stack = messaging_stack
    service = OtpService(
        stack.settings,
        stack.database,
        stack.event_platform.publisher,
        stack.service,
        stack.customers,
        stack.conversations,
    )

    _counter = itertools.count(1)

    def _unique_provider_response(_entry: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        provider_id = f"otp-prov-{next(_counter)}"
        return 200, {"messages": [{"id": provider_id}], "message_id": provider_id}

    stack.transport.set_handler(_unique_provider_response)

    class _OtpStack:
        def __init__(self) -> None:
            self.service = service
            self.messaging = stack.service
            self.transport = stack.transport
            self.database = stack.database
            self.settings = stack.settings
            self.event_platform = stack.event_platform
            self.customers = stack.customers
            self.conversations = stack.conversations

    return _OtpStack()
