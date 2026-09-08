"""Generic engineering lifecycle state-machine scenarios (MASTER PROMPT 002 section 9)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.nxs_close.__main__ import close
from scripts.nxs_control.core import (
    ControlError,
    eligible_phases,
    evaluate_guard,
    load_json,
    next_allowed_execution,
    next_eligible_phase,
    validate_invariants,
    write_json,
)
from scripts.nxs_guard import lock as lock_module
from scripts.nxs_start.__main__ import start
from scripts.nxs_state.__main__ import transition

PHASE_BRANCH = {
    "NXS-P01": "feat/nxs-p01-backend-core",
    "NXS-P02": "feat/nxs-p02-tenancy",
    "NXS-P03": "feat/nxs-p03-security-auth",
}


def _registry(repo: Path) -> dict[str, dict]:
    return {p["id"]: p for p in load_json(repo / ".nxs/phase-registry.json")["phases"]}


def _state(repo: Path) -> dict:
    return load_json(repo / ".nxs/project-state.json")


def _advance_to_ready(
    repo: Path,
    phase_id: str,
    checkout: Callable[[str], None],
    seed_evidence: Callable[..., None],
    commit_all: Callable[[str], str],
) -> str:
    checkout(PHASE_BRANCH[phase_id])
    start(phase_id, "codex", root=repo)
    seed_evidence(phase_id)
    transition(phase_id, "VALIDATING", root=repo)
    sha = commit_all(f"impl {phase_id}")
    close(phase_id, sha, root=repo)
    return sha


# --- Scenario 1 + 2: P00 READY -> start P01 ---
def test_scenario_1_and_2_start_p01(
    lifecycle_repo: Path, seed_evidence: Callable[..., None]
) -> None:
    result = start("NXS-P01", "codex", root=lifecycle_repo)
    assert result["status"] == "BUILDING"
    state = load_json(lifecycle_repo / ".nxs/project-state.json")
    assert state["current_phase"]["id"] == "NXS-P01"
    assert state["current_phase"]["branch"] == "feat/nxs-p01-backend-core"
    assert state["active_phase"] == "NXS-P01"
    assert state["current_phase"]["status"] == "BUILDING"
    registry = _registry(lifecycle_repo)
    assert registry["NXS-P01"]["status"] == "BUILDING"
    lock = load_json(lifecycle_repo / ".nxs/execution-lock.json")
    assert lock["state"] == "ACTIVE" and lock["phase"] == "NXS-P01" and lock["actor"] == "codex"
    requirements = {
        r["id"]: r for r in load_json(lifecycle_repo / ".nxs/requirements.json")["requirements"]
    }
    assert requirements["NXS-PLATFORM-002"]["status"] == "IN_PROGRESS"
    validate_invariants(lifecycle_repo)


# --- Scenario 3: phase dependency invalid -> BLOCK ---
def test_scenario_3_invalid_dependency_blocks(
    lifecycle_repo: Path, checkout: Callable[[str], None]
) -> None:
    checkout("feat/nxs-p02-tenancy")
    guard = evaluate_guard(lifecycle_repo, "NXS-P02", "feat/nxs-p02-tenancy")
    assert guard.result == "BLOCK"
    assert any("dependency NXS-P01" in reason for reason in guard.reasons)
    with pytest.raises(ControlError, match="dependency NXS-P01"):
        start("NXS-P02", "codex", root=lifecycle_repo)


# --- Scenario 4: P01 closure -> READY/GO ---
def test_scenario_4_closure(
    lifecycle_repo: Path,
    seed_evidence: Callable[..., None],
    commit_all: Callable[[str], str],
) -> None:
    start("NXS-P01", "codex", root=lifecycle_repo)
    seed_evidence("NXS-P01", passed=120)
    transition("NXS-P01", "VALIDATING", root=lifecycle_repo)
    sha = commit_all("impl NXS-P01")
    result = close("NXS-P01", sha, root=lifecycle_repo)
    assert result["decision"] == "GO"
    registry = _registry(lifecycle_repo)
    assert registry["NXS-P01"]["status"] == "READY"
    assert registry["NXS-P01"]["decision"] == "GO"
    state = load_json(lifecycle_repo / ".nxs/project-state.json")
    assert state["current_phase"]["id"] == "NXS-P01"
    assert state["current_phase"]["implementation_commit"] == sha
    assert state["active_phase"] is None
    assert "NXS-P01" in state["completed_phases"]
    readiness = {e["phase"]: e for e in load_json(lifecycle_repo / ".nxs/readiness.json")["phases"]}
    assert readiness["NXS-P01"]["test_count"] == 120
    assert load_json(lifecycle_repo / ".nxs/execution-lock.json")["state"] == "RELEASED"


# --- Scenario 5: after P01 closure -> next allowed phase is P02 ---
def test_scenario_5_next_phase_is_p02(
    lifecycle_repo: Path,
    seed_evidence: Callable[..., None],
    commit_all: Callable[[str], str],
) -> None:
    start("NXS-P01", "codex", root=lifecycle_repo)
    seed_evidence("NXS-P01")
    transition("NXS-P01", "VALIDATING", root=lifecycle_repo)
    close("NXS-P01", commit_all("impl NXS-P01"), root=lifecycle_repo)
    nae = _state(lifecycle_repo)["next_allowed_execution"]
    assert nae == {"phase": "NXS-P02", "condition": "DEPENDENCIES_READY"}
    assert next_eligible_phase(lifecycle_repo) == "NXS-P02"


# --- Scenario 6: simulated P02 start/closure -> current becomes P02, next becomes P03 ---
def test_scenario_6_p02_then_p03(
    lifecycle_repo: Path,
    checkout: Callable[[str], None],
    seed_evidence: Callable[..., None],
    commit_all: Callable[[str], str],
) -> None:
    _advance_to_ready(lifecycle_repo, "NXS-P01", checkout, seed_evidence, commit_all)
    _advance_to_ready(lifecycle_repo, "NXS-P02", checkout, seed_evidence, commit_all)
    state = load_json(lifecycle_repo / ".nxs/project-state.json")
    assert state["current_phase"]["id"] == "NXS-P02"
    assert state["current_phase"]["branch"] == "feat/nxs-p02-tenancy"
    assert state["next_allowed_execution"]["phase"] == "NXS-P03"


# --- Scenario 7: no hardcoded P01 behaviour remains ---
def test_scenario_7_no_hardcoded_phase_ids() -> None:
    root = Path(__file__).parents[2]
    for module in ("nxs_close", "nxs_start", "nxs_state", "nxs_gate"):
        source = (root / "scripts" / module / "__main__.py").read_text(encoding="utf-8")
        assert '"NXS-P01"' not in source
        assert "'NXS-P01'" not in source
    core = (root / "scripts/nxs_control/core.py").read_text(encoding="utf-8")
    assert "NXS-P01" not in core


# --- Scenario 8: conflicting active phase -> BLOCK ---
def test_scenario_8_conflicting_active_phase(lifecycle_repo: Path) -> None:
    state = load_json(lifecycle_repo / ".nxs/project-state.json")
    state["active_phase"] = "NXS-P02"
    write_json(lifecycle_repo / ".nxs/project-state.json", state)
    with pytest.raises(ControlError, match="conflicting active phase"):
        start("NXS-P01", "codex", root=lifecycle_repo)


# --- Scenario 9: stale lock -> controlled recovery only ---
def test_scenario_9_stale_lock_controlled_recovery(
    lifecycle_repo: Path, commit_all: Callable[[str], str]
) -> None:
    start("NXS-P01", "codex", root=lifecycle_repo)
    lock_path = lifecycle_repo / ".nxs/execution-lock.json"
    lock = load_json(lock_path)
    lock["expires_at"] = "2000-01-01T00:00:00Z"
    write_json(lock_path, lock)
    commit_all("activate NXS-P01 with stale lock")
    guard = evaluate_guard(lifecycle_repo, "NXS-P01", "feat/nxs-p01-backend-core")
    assert any("stale" in reason for reason in guard.reasons)
    # A dirty tree blocks recovery; a clean tree permits it.
    (lifecycle_repo / "dirty.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ControlError, match="clean working tree"):
        lock_module.recover(lifecycle_repo)
    (lifecycle_repo / "dirty.txt").unlink()
    lock_module.recover(lifecycle_repo)
    assert load_json(lock_path)["state"] == "RELEASED"


# --- Scenario 10: READY phase restart without delta -> BLOCK ---
def test_scenario_10_ready_restart_blocked(
    lifecycle_repo: Path,
    seed_evidence: Callable[..., None],
    commit_all: Callable[[str], str],
) -> None:
    start("NXS-P01", "codex", root=lifecycle_repo)
    seed_evidence("NXS-P01")
    transition("NXS-P01", "VALIDATING", root=lifecycle_repo)
    close("NXS-P01", commit_all("impl NXS-P01"), root=lifecycle_repo)
    with pytest.raises(ControlError, match="guard blocks start"):
        start("NXS-P01", "codex", root=lifecycle_repo)
    guard = evaluate_guard(lifecycle_repo, "NXS-P01", "feat/nxs-p01-backend-core")
    assert guard.code == "ALREADY_READY"


# --- Scenario 11: malformed phase manifest -> BLOCK ---
def test_scenario_11_malformed_manifest_blocks(lifecycle_repo: Path) -> None:
    manifest_path = lifecycle_repo / ".nxs/phases/NXS-P01.json"
    manifest = load_json(manifest_path)
    manifest["requirements_implemented"] = ["NXS-PLATFORM-002"]
    write_json(manifest_path, manifest)
    with pytest.raises(ControlError, match="mapping mismatch"):
        start("NXS-P01", "codex", root=lifecycle_repo)


# --- Scenario 12: future phase with unmet dependency -> BLOCK ---
def test_scenario_12_future_phase_unmet_dependency(lifecycle_repo: Path) -> None:
    guard = evaluate_guard(lifecycle_repo, "NXS-P05", "feat/nxs-p05-provisioner-dashboard")
    assert guard.result == "BLOCK"
    assert any("NXS-P04" in reason for reason in guard.reasons)
    assert "NXS-P05" not in eligible_phases(lifecycle_repo)


# --- Scenario 13: two technically eligible phases -> deterministic selection ---
def test_scenario_13_deterministic_selection(tmp_path: Path) -> None:
    nxs = tmp_path / ".nxs"
    nxs.mkdir()
    (tmp_path / ".git").mkdir()
    (nxs / "phase-registry.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "phases": [
                    {
                        "id": "NXS-P00",
                        "title": "Root",
                        "status": "READY",
                        "decision": "GO",
                        "dependencies": [],
                        "branch": "b0",
                    },
                    {
                        "id": "NXS-P06",
                        "title": "Beta",
                        "status": "PLANNED",
                        "decision": "PENDING",
                        "dependencies": ["NXS-P00"],
                        "branch": "b6",
                    },
                    {
                        "id": "NXS-P05",
                        "title": "Alpha",
                        "status": "PLANNED",
                        "decision": "PENDING",
                        "dependencies": ["NXS-P00"],
                        "branch": "b5",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (nxs / "project-state.json").write_text(
        json.dumps({"active_phase": None, "blocked_phases": []}), encoding="utf-8"
    )
    assert eligible_phases(tmp_path) == ["NXS-P06", "NXS-P05"]
    assert next_eligible_phase(tmp_path) == "NXS-P06"
    assert next_allowed_execution(tmp_path) == {
        "phase": "NXS-P06",
        "condition": "DEPENDENCIES_READY",
    }


def test_lock_cli_and_status(lifecycle_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.nxs_guard.__main__ import main as guard_main

    monkeypatch.chdir(lifecycle_repo)
    assert guard_main(["lock", "status", "--json"]) == 0
    assert guard_main(["lock", "acquire", "--phase", "NXS-P01", "--actor", "codex"]) == 0
    assert lock_module.status(lifecycle_repo)["state"] == "ACTIVE"
    assert guard_main(["lock", "acquire", "--phase", "NXS-P02", "--actor", "other"]) == 2
    assert guard_main(["lock", "release", "--phase", "NXS-P01"]) == 0
    assert lock_module.status(lifecycle_repo)["state"] == "RELEASED"


def test_guard_current_phase_cli(lifecycle_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.nxs_guard.__main__ import main as guard_main

    monkeypatch.chdir(lifecycle_repo)
    assert guard_main(["current-phase"]) == 0
    assert guard_main(["phase-order"]) == 0


def test_current_phase_prefers_local_branch_over_ci_ref_name(
    lifecycle_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression: push-to-main CI leaks GITHUB_REF_NAME=main; the repository's own
    checked-out branch (feat/nxs-p01-backend-core here) must stay authoritative."""
    from scripts.nxs_guard.__main__ import main as guard_main

    monkeypatch.chdir(lifecycle_repo)
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)

    assert guard_main(["current-phase"]) == 0
    assert capsys.readouterr().out.strip() == "NXS-P01"


def test_current_phase_prefers_local_branch_over_ci_head_ref(
    lifecycle_repo: Path,
    checkout: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.nxs_guard.__main__ import main as guard_main

    checkout("feat/nxs-p02-tenancy")
    monkeypatch.chdir(lifecycle_repo)
    monkeypatch.setenv("GITHUB_HEAD_REF", "some-unrelated-branch")
    monkeypatch.delenv("GITHUB_REF_NAME", raising=False)

    assert guard_main(["current-phase"]) == 0
    assert capsys.readouterr().out.strip() == "NXS-P02"


def test_current_phase_falls_back_to_ci_branch_on_detached_head(
    lifecycle_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Detached-HEAD CI checkout: `git branch --show-current` is empty, so the CI
    branch context is the intended fallback."""
    from scripts.nxs_guard.__main__ import main as guard_main

    subprocess.run(  # noqa: S603
        ["git", "-C", str(lifecycle_repo), "checkout", "-q", "--detach"],  # noqa: S607
        check=True,
        capture_output=True,
    )
    monkeypatch.chdir(lifecycle_repo)
    monkeypatch.delenv("GITHUB_REF_NAME", raising=False)
    monkeypatch.setenv("GITHUB_HEAD_REF", "feat/nxs-p02-tenancy")

    assert guard_main(["current-phase"]) == 0
    assert capsys.readouterr().out.strip() == "NXS-P02"


def test_current_phase_returns_nonzero_on_non_phase_branch(
    lifecycle_repo: Path,
    checkout: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.nxs_guard.__main__ import main as guard_main

    checkout("main")
    monkeypatch.chdir(lifecycle_repo)
    monkeypatch.delenv("GITHUB_REF_NAME", raising=False)
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)

    assert guard_main(["current-phase"]) == 1


def test_run_guard_infer_phase_still_uses_ci_branch_context(
    lifecycle_repo: Path,
    checkout: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """run_guard --infer-phase is unchanged: an explicit CI/--branch value still drives
    phase resolution even when the local checked-out branch differs."""
    from scripts.nxs_guard.__main__ import main as guard_main

    checkout("feat/nxs-p02-tenancy")
    monkeypatch.chdir(lifecycle_repo)

    assert guard_main(["--infer-phase", "--branch", "feat/nxs-p01-backend-core", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["phase"] == "NXS-P01"


def test_phase_order_cli_is_deterministic_registry_order(
    lifecycle_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.nxs_guard.__main__ import main as guard_main

    monkeypatch.chdir(lifecycle_repo)
    assert guard_main(["phase-order"]) == 0
    printed = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert printed[:3] == ["NXS-P00", "NXS-P01", "NXS-P02"]
    assert printed == sorted(printed)


def test_start_cli_json(lifecycle_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.nxs_start.__main__ import main as start_main

    monkeypatch.chdir(lifecycle_repo)
    argv = ["nxs_start", "--phase", "NXS-P01", "--actor", "codex", "--json"]
    monkeypatch.setattr("sys.argv", argv)
    assert start_main() == 0


def test_close_rejects_missing_commit(lifecycle_repo: Path) -> None:
    start("NXS-P01", "codex", root=lifecycle_repo)
    with pytest.raises(ControlError, match="not present in Git"):
        close("NXS-P01", "0" * 40, root=lifecycle_repo)


def test_transition_updates_only_matching_current_phase(lifecycle_repo: Path) -> None:
    transition("NXS-P02", "BLOCKED", root=lifecycle_repo)
    state = _state(lifecycle_repo)
    assert state["current_phase"]["id"] == "NXS-P00"
    registry = _registry(lifecycle_repo)
    assert registry["NXS-P02"]["status"] == "BLOCKED"


def test_repo_is_reachable_by_subprocess(lifecycle_repo: Path) -> None:
    assert (
        subprocess.run(  # noqa: S603
            ["git", "-C", str(lifecycle_repo), "rev-parse", "--is-inside-work-tree"],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "true"
    )


def test_reopen_closed_phase_for_corrective_delta(
    lifecycle_repo: Path,
    seed_evidence: Callable[..., None],
    commit_all: Callable[[str], str],
) -> None:
    # Close NXS-P01.
    start("NXS-P01", "codex", root=lifecycle_repo)
    seed_evidence("NXS-P01")
    transition("NXS-P01", "VALIDATING", root=lifecycle_repo)
    close("NXS-P01", commit_all("impl NXS-P01"), root=lifecycle_repo)
    assert _registry(lifecycle_repo)["NXS-P01"]["status"] == "READY"

    # Reopen it (audit finding) — READY -> VALIDATING clears the closure state.
    transition("NXS-P01", "VALIDATING", root=lifecycle_repo)
    state = _state(lifecycle_repo)
    registry = _registry(lifecycle_repo)
    assert registry["NXS-P01"]["status"] == "VALIDATING"
    assert registry["NXS-P01"]["decision"] == "PENDING"
    assert state["current_phase"]["id"] == "NXS-P01"
    assert state["current_phase"]["status"] == "VALIDATING"
    assert state["current_phase"]["implementation_commit"] is None
    assert "NXS-P01" not in state["completed_phases"]
    assert state["active_phase"] == "NXS-P01"
    assert state["next_allowed_execution"] == {
        "phase": "NXS-P01",
        "condition": "ACTIVE_PHASE_ONLY",
    }
    readiness = {e["phase"] for e in load_json(lifecycle_repo / ".nxs/readiness.json")["phases"]}
    assert "NXS-P01" not in readiness
    validate_invariants(lifecycle_repo)

    # Re-close with a corrective commit.
    seed_evidence("NXS-P01", passed=99)
    close("NXS-P01", commit_all("corrective NXS-P01"), root=lifecycle_repo)
    reclosed = _registry(lifecycle_repo)["NXS-P01"]
    assert reclosed["status"] == "READY"
    assert reclosed["decision"] == "GO"
    assert _state(lifecycle_repo)["next_allowed_execution"]["phase"] == "NXS-P02"


def test_reopen_is_the_only_transition_out_of_ready() -> None:
    from scripts.nxs_control.core import REOPEN_TRANSITION, transition_allowed

    assert REOPEN_TRANSITION == ("READY", "VALIDATING")
    assert transition_allowed("READY", "VALIDATING")
    for target in ("READY", "BUILDING", "FAILED", "BLOCKED", "READY_TO_EXECUTE"):
        assert not transition_allowed("READY", target)
