"""Coverage for the control-system command entrypoints."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.nxs_close.__main__ import main as close_main
from scripts.nxs_gate.__main__ import run_gate
from scripts.nxs_guard.__main__ import main as guard_main
from scripts.nxs_start.__main__ import start
from scripts.nxs_state.__main__ import main as state_main
from scripts.nxs_state.__main__ import transition
from scripts.nxs_validate.__main__ import main as validate_main


def test_validate_main_on_real_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(Path(__file__).parents[2])
    assert validate_main() == 0


def test_validate_main_reports_failure(
    lifecycle_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (lifecycle_repo / ".nxs/project-state.json").write_text("{", encoding="utf-8")
    monkeypatch.chdir(lifecycle_repo)
    assert validate_main() == 1


def test_guard_main_json_and_text(lifecycle_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(lifecycle_repo)
    assert guard_main(["--phase", "NXS-P01", "--branch", "feat/nxs-p01-backend-core"]) == 0
    assert guard_main(["--phase", "NXS-P01", "--branch", "wrong", "--json"]) == 2
    assert guard_main(["--infer-phase", "--branch", "feat/nxs-p01-backend-core"]) == 0
    assert guard_main(["--infer-phase", "--branch", "not-a-phase-branch"]) == 2


def test_state_main_accept_and_reject(
    lifecycle_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(lifecycle_repo)
    monkeypatch.setattr("sys.argv", ["nxs_state", "NXS-P02", "BLOCKED"])
    assert state_main() == 0
    monkeypatch.setattr("sys.argv", ["nxs_state", "NXS-P00", "READY"])
    assert state_main() == 2


def test_close_main_blocks_without_evidence(
    lifecycle_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start("NXS-P01", "codex", root=lifecycle_repo)
    transition("NXS-P01", "VALIDATING", root=lifecycle_repo)
    monkeypatch.chdir(lifecycle_repo)
    monkeypatch.setattr(
        "sys.argv", ["nxs_close", "--phase", "NXS-P01", "--implementation-commit", "a" * 40]
    )
    assert close_main() == 2


def test_close_main_succeeds(
    lifecycle_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_evidence: Callable[..., None],
    commit_all: Callable[[str], str],
) -> None:
    start("NXS-P01", "codex", root=lifecycle_repo)
    seed_evidence("NXS-P01")
    transition("NXS-P01", "VALIDATING", root=lifecycle_repo)
    sha = commit_all("impl")
    monkeypatch.chdir(lifecycle_repo)
    monkeypatch.setattr(
        "sys.argv", ["nxs_close", "--phase", "NXS-P01", "--implementation-commit", sha]
    )
    assert close_main() == 0


def test_run_gate_aggregates(lifecycle_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    evidence = run_gate(lifecycle_repo, "NXS-P01")
    assert evidence["result"] == "PASS"
    assert (lifecycle_repo / ".nxs/evidence/NXS-P01/quality-gates.json").exists()
    assert calls


def test_run_gate_reports_failure(lifecycle_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    evidence = run_gate(lifecycle_repo, "NXS-P01")
    assert evidence["result"] == "FAIL"
    assert evidence["decision_eligibility"] == "NO_GO"
