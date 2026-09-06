from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts.nxs_control.core import (
    ControlError,
    evaluate_guard,
    load_json,
    repository_root,
    transition_allowed,
    validate_invariants,
)
from scripts.nxs_guard.lock import acquire, recover, release


@pytest.fixture
def control_repo(tmp_path: Path) -> Path:
    source = Path(__file__).parents[2]
    shutil.copytree(source / ".nxs", tmp_path / ".nxs")
    (tmp_path / ".git").mkdir()
    set_phase(tmp_path, "NXS-P00", "BUILDING", "PENDING")
    state_path = tmp_path / ".nxs/project-state.json"
    state = load_json(state_path)
    state["current_phase"]["status"] = "BUILDING"
    state["current_phase"]["decision"] = "PENDING"
    state["completed_phases"] = []
    state["active_phase"] = "NXS-P00"
    write(state_path, state)
    manifest_path = tmp_path / ".nxs/phases/NXS-P00.json"
    manifest = load_json(manifest_path)
    manifest["status"] = "BUILDING"
    manifest["decision"] = "PENDING"
    write(manifest_path, manifest)
    readiness = load_json(tmp_path / ".nxs/readiness.json")
    readiness["phases"] = []
    write(tmp_path / ".nxs/readiness.json", readiness)
    return tmp_path


def write(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def set_phase(control_repo: Path, phase_id: str, status: str, decision: str) -> None:
    registry_path = control_repo / ".nxs/phase-registry.json"
    registry = load_json(registry_path)
    phase = next(item for item in registry["phases"] if item["id"] == phase_id)
    phase["status"] = status
    phase["decision"] = decision
    write(registry_path, registry)


def add_ready_evidence(control_repo: Path, phase_id: str) -> None:
    readiness_path = control_repo / ".nxs/readiness.json"
    readiness = load_json(readiness_path)
    readiness["phases"].append(
        {
            "phase": phase_id,
            "branch": "test",
            "implementation_commit": "a" * 40,
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
    )
    write(readiness_path, readiness)


def test_scenario_a_ready_phase_is_already_ready_block(control_repo: Path) -> None:
    set_phase(control_repo, "NXS-P00", "READY", "GO")
    add_ready_evidence(control_repo, "NXS-P00")
    state_path = control_repo / ".nxs/project-state.json"
    state = load_json(state_path)
    state["current_phase"]["status"] = "READY"
    state["current_phase"]["decision"] = "GO"
    state["completed_phases"] = ["NXS-P00"]
    state["active_phase"] = None
    write(state_path, state)
    manifest_path = control_repo / ".nxs/phases/NXS-P00.json"
    manifest = load_json(manifest_path)
    manifest["status"] = "READY"
    manifest["decision"] = "GO"
    write(manifest_path, manifest)
    result = evaluate_guard(control_repo, "NXS-P00", "feat/nxs-p00-engineering-control-system")
    assert result.result == "BLOCK"
    assert result.code == "ALREADY_READY"


def test_scenario_b_failed_dependency_blocks(control_repo: Path) -> None:
    set_phase(control_repo, "NXS-P00", "FAILED", "NO_GO")
    result = evaluate_guard(control_repo, "NXS-P01", "feat/nxs-p01-backend-core")
    assert result.result == "BLOCK"


def test_scenario_c_ready_dependency_passes(control_repo: Path) -> None:
    set_phase(control_repo, "NXS-P00", "READY", "GO")
    add_ready_evidence(control_repo, "NXS-P00")
    state_path = control_repo / ".nxs/project-state.json"
    state = load_json(state_path)
    state["current_phase"]["status"] = "READY"
    state["current_phase"]["decision"] = "GO"
    state["completed_phases"] = ["NXS-P00"]
    state["active_phase"] = None
    write(state_path, state)
    manifest_path = control_repo / ".nxs/phases/NXS-P00.json"
    manifest = load_json(manifest_path)
    manifest["status"] = "READY"
    manifest["decision"] = "GO"
    write(manifest_path, manifest)
    result = evaluate_guard(control_repo, "NXS-P01", "feat/nxs-p01-backend-core")
    assert result.result == "PASS"


def test_scenario_d_malformed_project_state_blocks(control_repo: Path) -> None:
    (control_repo / ".nxs/project-state.json").write_text("{", encoding="utf-8")
    result = evaluate_guard(control_repo, "NXS-P00", "feat/nxs-p00-engineering-control-system")
    assert result.result == "BLOCK"
    assert result.code == "MALFORMED_STATE"


def test_scenario_e_unknown_status_fails_schema(control_repo: Path) -> None:
    state_path = control_repo / ".nxs/project-state.json"
    state = load_json(state_path)
    state["current_phase"]["status"] = "MAYBE"
    write(state_path, state)
    with pytest.raises(ControlError, match="schema validation failed"):
        validate_invariants(control_repo)


def test_scenario_f_ready_with_failed_gate_fails(control_repo: Path) -> None:
    set_phase(control_repo, "NXS-P00", "READY", "GO")
    add_ready_evidence(control_repo, "NXS-P00")
    readiness_path = control_repo / ".nxs/readiness.json"
    readiness = load_json(readiness_path)
    readiness["phases"][0]["gates"]["tests"] = "FAIL"
    write(readiness_path, readiness)
    with pytest.raises(ControlError, match="all gates PASS"):
        validate_invariants(control_repo)


def test_scenario_g_ready_with_unready_dependency_fails(control_repo: Path) -> None:
    set_phase(control_repo, "NXS-P01", "READY", "GO")
    add_ready_evidence(control_repo, "NXS-P01")
    with pytest.raises(ControlError, match="dependency NXS-P00 READY/GO"):
        validate_invariants(control_repo)


def test_scenario_h_invalid_transition_rejected() -> None:
    assert not transition_allowed("PLANNED", "READY")
    assert transition_allowed("PLANNED", "READY_TO_EXECUTE")


def test_scenario_i_stale_lock_requires_controlled_recovery(
    control_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = control_repo / ".nxs/execution-lock.json"
    lock = load_json(lock_path)
    lock.update(
        {
            "state": "ACTIVE",
            "phase": "NXS-P00",
            "branch": "branch",
            "actor": "Agent A",
            "started_at": "2026-01-01T00:00:00Z",
            "expires_at": "2026-01-01T01:00:00Z",
            "repository_commit": "a" * 40,
        }
    )
    write(lock_path, lock)
    monkeypatch.setattr("scripts.nxs_guard.lock.git", lambda *_args: "")
    recover(control_repo)
    assert load_json(lock_path)["state"] == "RELEASED"


def test_lock_acquire_and_release(control_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "scripts.nxs_guard.lock.git",
        lambda _root, command, *_args: "branch" if command == "branch" else "a" * 40,
    )
    acquire(control_repo, "NXS-P00", "Agent A")
    assert load_json(control_repo / ".nxs/execution-lock.json")["state"] == "ACTIVE"
    with pytest.raises(ControlError, match="already active"):
        acquire(control_repo, "NXS-P00", "Agent B")
    release(control_repo, "NXS-P00")


def test_recover_rejects_fresh_lock(control_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock_path = control_repo / ".nxs/execution-lock.json"
    lock = load_json(lock_path)
    lock.update(
        {
            "state": "ACTIVE",
            "phase": "NXS-P00",
            "branch": "branch",
            "actor": "Agent A",
            "started_at": "2026-01-01T00:00:00Z",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "repository_commit": "a" * 40,
        }
    )
    write(lock_path, lock)
    monkeypatch.setattr("scripts.nxs_guard.lock.git", lambda *_args: "")
    with pytest.raises(ControlError, match="has not expired"):
        recover(control_repo)


def test_scenario_j_missing_mandatory_mapping_fails(control_repo: Path) -> None:
    manifest_path = control_repo / ".nxs/phases/NXS-P00.json"
    manifest = load_json(manifest_path)
    manifest["requirements_implemented"].pop()
    write(manifest_path, manifest)
    with pytest.raises(ControlError, match="mapping mismatch"):
        validate_invariants(control_repo)


def test_load_json_rejects_array(tmp_path: Path) -> None:
    path = tmp_path / "array.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ControlError, match="expected JSON object"):
        load_json(path)


def test_repository_root_walks_parents(control_repo: Path) -> None:
    nested = control_repo / "one/two"
    nested.mkdir(parents=True)
    assert repository_root(nested) == control_repo


def test_guard_rejects_unknown_phase(control_repo: Path) -> None:
    result = evaluate_guard(control_repo, "NXS-P99", "branch")
    assert result.code == "MALFORMED_STATE"


def test_guard_reports_wrong_branch_and_conflicting_active_phase(control_repo: Path) -> None:
    state_path = control_repo / ".nxs/project-state.json"
    state = load_json(state_path)
    state["active_phase"] = "NXS-P02"
    write(state_path, state)
    result = evaluate_guard(control_repo, "NXS-P01", "wrong")
    assert result.code == "PRECONDITION_FAILED"
    assert len(result.reasons) >= 2


def test_guard_reports_stale_same_phase_lock(control_repo: Path) -> None:
    lock_path = control_repo / ".nxs/execution-lock.json"
    lock = load_json(lock_path)
    lock.update(
        {
            "state": "ACTIVE",
            "phase": "NXS-P00",
            "branch": "branch",
            "actor": "Agent A",
            "started_at": "2026-01-01T00:00:00Z",
            "expires_at": "2026-01-01T01:00:00Z",
            "repository_commit": "a" * 40,
        }
    )
    write(lock_path, lock)
    result = evaluate_guard(control_repo, "NXS-P00", "feat/nxs-p00-engineering-control-system")
    assert any("stale" in reason for reason in result.reasons)
