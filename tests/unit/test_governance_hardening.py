"""H0 admission, durable deferral and future certification invariants."""

import copy
from pathlib import Path

import pytest

from scripts.nxs_close.__main__ import close
from scripts.nxs_control.core import (
    ControlError,
    evaluate_guard,
    git,
    load_json,
    next_eligible_phase,
    registry_phases,
    validate_invariants,
    write_json,
)
from scripts.nxs_control.obligations import dependency_closure
from scripts.nxs_start.__main__ import start
from scripts.nxs_state.__main__ import transition


def snapshot(repo):
    return {
        str(path.relative_to(repo)): path.read_bytes() for path in (repo / ".nxs").rglob("*.json")
    }


def test_later_dependency_qualified_phase_cannot_skip_next(lifecycle_repo, checkout):
    path = lifecycle_repo / ".nxs/phase-registry.json"
    registry = load_json(path)
    later = next(phase for phase in registry["phases"] if phase["id"] == "NXS-P05")
    later["dependencies"] = ["NXS-P00"]
    write_json(path, registry)
    checkout(later["branch"])
    before = snapshot(lifecycle_repo)
    assert next_eligible_phase(lifecycle_repo) == "NXS-P01"
    guard = evaluate_guard(lifecycle_repo, "NXS-P05")
    assert guard.result == "BLOCK"
    assert "requested phase NXS-P05; expected next eligible phase NXS-P01" in guard.reasons
    with pytest.raises(ControlError, match="expected next eligible phase NXS-P01"):
        start("NXS-P05", "codex", root=lifecycle_repo)
    assert snapshot(lifecycle_repo) == before


def test_skip_cli_fails_before_any_mutation(lifecycle_repo, checkout, monkeypatch, capsys):
    from scripts.nxs_start.__main__ import main

    checkout("feat/nxs-p23-metering")
    monkeypatch.chdir(lifecycle_repo)
    monkeypatch.setattr("sys.argv", ["nxs_start", "--phase", "NXS-P23", "--actor", "codex"])
    before = snapshot(lifecycle_repo)
    assert main() == 2
    assert "expected next eligible phase NXS-P01" in capsys.readouterr().out
    assert snapshot(lifecycle_repo) == before


def test_active_continuation_is_guarded_idempotent(lifecycle_repo, checkout):
    start("NXS-P01", "codex", root=lifecycle_repo)
    before = snapshot(lifecycle_repo)
    assert start("NXS-P01", "codex", root=lifecycle_repo)["note"] == "already active"
    assert snapshot(lifecycle_repo) == before
    checkout("main")
    with pytest.raises(ControlError, match="branch"):
        start("NXS-P01", "codex", root=lifecycle_repo)


@pytest.mark.parametrize("field", ["source_phase", "owner_phase", "blocks_phases"])
def test_unknown_obligation_phases_fail_closed(lifecycle_repo, field):
    path = lifecycle_repo / ".nxs/deferred-obligations.json"
    document = load_json(path)
    document["obligations"][0][field] = ["NXS-P99"] if field == "blocks_phases" else "NXS-P99"
    write_json(path, document)
    with pytest.raises(ControlError, match="unknown phase"):
        validate_invariants(lifecycle_repo)


def test_duplicate_obligation_rejected(lifecycle_repo):
    path = lifecycle_repo / ".nxs/deferred-obligations.json"
    document = load_json(path)
    document["obligations"].append(copy.deepcopy(document["obligations"][0]))
    write_json(path, document)
    with pytest.raises(ControlError, match="duplicate obligation"):
        validate_invariants(lifecycle_repo)


@pytest.mark.parametrize("mutation", ["status", "history", "open_resolution", "resolved_empty"])
def test_invalid_obligation_status_data_rejected(lifecycle_repo, mutation):
    path = lifecycle_repo / ".nxs/deferred-obligations.json"
    document = load_json(path)
    obligation = document["obligations"][0]
    if mutation == "status":
        obligation["status"] = "WAIVED"
    elif mutation == "history":
        obligation["status_history"] = ["OPEN", "OPEN"]
    elif mutation == "open_resolution":
        obligation["resolution"] = {"implementation_commit": "a" * 40, "evidence": ["fake.json"]}
    else:
        obligation.update(status="RESOLVED", status_history=["OPEN", "RESOLVED"])
    write_json(path, document)
    with pytest.raises(ControlError):
        validate_invariants(lifecycle_repo)


def test_unfinished_owner_cannot_resolve_obligation(lifecycle_repo):
    path = lifecycle_repo / ".nxs/deferred-obligations.json"
    document = load_json(path)
    document["obligations"][0].update(
        status="RESOLVED",
        status_history=["OPEN", "RESOLVED"],
        resolution={"implementation_commit": "a" * 40, "evidence": ["evidence.json"]},
    )
    write_json(path, document)
    with pytest.raises(ControlError, match="resolution requires owner"):
        validate_invariants(lifecycle_repo)


def test_resolution_binds_completed_owner_commit_and_evidence(
    lifecycle_repo, seed_evidence, commit_all
):
    start("NXS-P01", "codex", root=lifecycle_repo)
    seed_evidence("NXS-P01")
    transition("NXS-P01", "VALIDATING", root=lifecycle_repo)
    implementation = commit_all("fixture implementation")
    close("NXS-P01", implementation, root=lifecycle_repo)
    path = lifecycle_repo / ".nxs/deferred-obligations.json"
    document = load_json(path)
    obligation = document["obligations"][0]
    obligation.update(
        owner_phase="NXS-P01",
        certification_impact="TRACKED",
        status="RESOLVED",
        status_history=["OPEN", "IN_PROGRESS", "RESOLVED"],
        resolution={
            "implementation_commit": implementation,
            "evidence": [".nxs/evidence/NXS-P01/quality-matrix.json"],
        },
    )
    write_json(path, document)
    validate_invariants(lifecycle_repo)
    obligation["resolution"]["implementation_commit"] = "a" * 40
    write_json(path, document)
    with pytest.raises(ControlError, match="owning implementation"):
        validate_invariants(lifecycle_repo)
    obligation["resolution"]["implementation_commit"] = implementation
    obligation["resolution"]["evidence"] = ["unbound.json"]
    write_json(path, document)
    with pytest.raises(ControlError, match="evidence is missing"):
        validate_invariants(lifecycle_repo)


@pytest.mark.parametrize(
    "mutation",
    [
        "remove_block",
        "wrong_owner",
        "remove_ancestor",
        "unknown_policy",
        "duplicate_policy",
        "cycle",
    ],
)
def test_certification_policy_cannot_be_weakened(lifecycle_repo, mutation):
    path = lifecycle_repo / ".nxs/deferred-obligations.json"
    document = load_json(path)
    if mutation == "remove_block":
        document["obligations"][0]["blocks_phases"] = []
    elif mutation == "wrong_owner":
        document["obligations"][0]["owner_phase"] = "NXS-P31"
    elif mutation == "unknown_policy":
        document["certification_policies"][0]["id"] = "NXS-P99"
    elif mutation == "duplicate_policy":
        document["certification_policies"].append(
            copy.deepcopy(document["certification_policies"][0])
        )
    else:
        registry_path = lifecycle_repo / ".nxs/phase-registry.json"
        registry = load_json(registry_path)
        phase = next(item for item in registry["phases"] if item["id"] == "NXS-P30")
        if mutation == "remove_ancestor":
            phase["dependencies"].remove("NXS-P22")
        else:
            phase["dependencies"].append("NXS-P30")
        write_json(registry_path, registry)
    write_json(path, document)
    with pytest.raises(ControlError):
        validate_invariants(lifecycle_repo)


def certification_fixture(repo):
    path = repo / ".nxs/phase-registry.json"
    registry = load_json(path)
    readiness = load_json(repo / ".nxs/readiness.json")
    template = readiness["phases"][0]
    completed = []
    for phase in registry["phases"]:
        if phase["id"] >= "NXS-P30":
            continue
        completed.append(phase["id"])
        phase.update(status="READY", decision="GO")
        entry = copy.deepcopy(template)
        entry["phase"] = phase["id"]
        readiness["phases"] = [item for item in readiness["phases"] if item["phase"] != phase["id"]]
        readiness["phases"].append(entry)
        manifest_path = repo / f".nxs/phases/{phase['id']}.json"
        if manifest_path.exists():
            manifest = load_json(manifest_path)
            manifest.update(status="READY", decision="GO")
            write_json(manifest_path, manifest)
    write_json(path, registry)
    write_json(repo / ".nxs/readiness.json", readiness)
    state_path = repo / ".nxs/project-state.json"
    state = load_json(state_path)
    state["completed_phases"] = completed
    state["active_phase"] = None
    write_json(state_path, state)
    validate_invariants(repo)


def test_p30_guard_and_close_block_unresolved_production_obligations(lifecycle_repo, checkout):
    certification_fixture(lifecycle_repo)
    checkout("feat/nxs-p30-backend-certification")
    before = snapshot(lifecycle_repo)
    assert next_eligible_phase(lifecycle_repo) == "NXS-P30"
    assert evaluate_guard(lifecycle_repo, "NXS-P30").result == "BLOCK"
    with pytest.raises(ControlError, match="unresolved"):
        start("NXS-P30", "codex", root=lifecycle_repo)
    with pytest.raises(ControlError, match="closure blocked"):
        close("NXS-P30", git(lifecycle_repo, "rev-parse", "HEAD"), root=lifecycle_repo)
    transition("NXS-P30", "READY_TO_EXECUTE", root=lifecycle_repo)
    transition("NXS-P30", "BUILDING", root=lifecycle_repo)
    transition("NXS-P30", "VALIDATING", root=lifecycle_repo)
    validating = snapshot(lifecycle_repo)
    with pytest.raises(ControlError, match="closure blocked"):
        transition("NXS-P30", "READY", root=lifecycle_repo)
    assert snapshot(lifecycle_repo) == validating
    assert before[".nxs/requirements.json"] == validating[".nxs/requirements.json"]


def test_p30_ready_is_invalid_with_unresolved_obligations(lifecycle_repo):
    certification_fixture(lifecycle_repo)
    path = lifecycle_repo / ".nxs/phase-registry.json"
    registry = load_json(path)
    next(phase for phase in registry["phases"] if phase["id"] == "NXS-P30").update(
        status="READY", decision="GO"
    )
    write_json(path, registry)
    readiness_path = lifecycle_repo / ".nxs/readiness.json"
    readiness = load_json(readiness_path)
    readiness["phases"].append(readiness["phases"][0] | {"phase": "NXS-P30"})
    write_json(readiness_path, readiness)
    with pytest.raises(ControlError, match="READY blocked by unresolved"):
        validate_invariants(lifecycle_repo)


def test_historical_readiness_preserved_and_future_contracts_complete():
    root = Path(__file__).parents[2]
    validate_invariants(root)
    phases = registry_phases(root)
    assert "NXS-P22" in dependency_closure(phases, "NXS-P30")
    expected_requirements = {
        "NXS-P23": {"NXS-METER-001", "NXS-COST-001"},
        "NXS-P25": {"NXS-WF-002", "NXS-SCHED-002", "NXS-HUMAN-002", "NXS-SRE-002", "NXS-RES-003"},
        "NXS-P27": {"NXS-SEC-004", "NXS-SUPPLY-001"},
        "NXS-P28": {"NXS-CAP-002", "NXS-CAP-003"},
        "NXS-P29": {"NXS-CHAOS-001", "NXS-FAILOVER-001"},
        "NXS-P30": {"NXS-CERT-001", "NXS-CERT-002"},
        "NXS-P31": {"NXS-RELEASE-001"},
        "NXS-P32": {"NXS-DEPLOY-001", "NXS-DEPLOY-002"},
    }
    requirements = load_json(root / ".nxs/requirements.json")["requirements"]
    for phase_id, required_ids in expected_requirements.items():
        mandatory_ids = {
            item["id"]
            for item in requirements
            if item["mandatory"] and item["target_phase"] == phase_id
        }
        assert required_ids <= mandatory_ids
        assert phase_id in phases
