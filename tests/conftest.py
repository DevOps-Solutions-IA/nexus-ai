from __future__ import annotations

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


_IT_DEFAULTS = {
    "NXS_ENVIRONMENT": "test",
    "NXS_LOGGING__FORMAT": "json",
    "NXS_DATABASE__REQUIRED": "true",
    "NXS_CACHE__REQUIRED": "true",
    "NXS_MESSAGING__REQUIRED": "true",
    "NXS_DATABASE__DSN": os.environ.get(
        "NXS_IT_DATABASE_DSN",
        "postgresql+asyncpg://nexus_local:local-development-only@127.0.0.1:15432/nexus_local",
    ),
    "NXS_CACHE__URL": os.environ.get("NXS_IT_CACHE_URL", "redis://127.0.0.1:16379/0"),
    "NXS_MESSAGING__URL": os.environ.get("NXS_IT_MESSAGING_URL", "nats://127.0.0.1:14222"),
    "NXS_HEALTH__CACHE_TTL_SECONDS": "0",
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
