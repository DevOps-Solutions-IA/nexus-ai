from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).parents[1]

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


@pytest.fixture
def lifecycle_repo(tmp_path: Path) -> Iterator[Path]:
    """A throwaway Git repository seeded with the real .nxs control state at P00 READY/GO."""
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copytree(REPO_ROOT / ".nxs", repo / ".nxs")
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
