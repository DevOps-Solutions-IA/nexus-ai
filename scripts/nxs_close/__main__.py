from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, cast

from scripts.nxs_control.core import (
    ControlError,
    commit_exists,
    evaluate_guard,
    indexed,
    iso_now,
    load_json,
    next_allowed_execution,
    repository_root,
    validate_invariants,
    write_json,
)
from scripts.nxs_guard.lock import release_if_held


def _require_all_pass(evidence: dict[str, Any], label: str) -> None:
    if evidence.get("result") != "PASS" or any(
        value != "PASS" for value in evidence.get("gates", {}).values()
    ):
        raise ControlError(f"{label} is absent or failing")


def close(
    phase_id: str,
    implementation_commit: str,
    *,
    root: Path | None = None,
    verify_commit: bool = True,
) -> dict[str, Any]:
    root = root or repository_root()
    validate_invariants(root)
    guard = evaluate_guard(root, phase_id)
    if guard.result != "PASS":
        raise ControlError("guard does not pass: " + "; ".join(guard.reasons))
    if len(implementation_commit) != 40 or any(
        character not in "0123456789abcdef" for character in implementation_commit
    ):
        raise ControlError("implementation commit must be a full lowercase Git SHA")
    if verify_commit and not commit_exists(root, implementation_commit):
        raise ControlError(f"implementation commit {implementation_commit} is not present in Git")

    evidence_path = root / f".nxs/evidence/{phase_id}/quality-gates.json"
    _require_all_pass(load_json(evidence_path), "mandatory quality gate evidence")
    matrix_path = root / f".nxs/evidence/{phase_id}/quality-matrix.json"
    matrix = load_json(matrix_path)
    _require_all_pass(matrix, "complete quality matrix")

    manifest_path = root / f".nxs/phases/{phase_id}.json"
    manifest = load_json(manifest_path)
    if manifest["status"] != "VALIDATING":
        raise ControlError("phase must be VALIDATING before closure")

    requirements_path = root / ".nxs/requirements.json"
    requirements_doc = load_json(requirements_path)
    requirements = indexed(
        cast(list[dict[str, Any]], requirements_doc["requirements"]), "requirement"
    )
    evidence_ref = str(evidence_path.relative_to(root))
    for requirement_id in cast(list[str], manifest["requirements_implemented"]):
        requirement = requirements[requirement_id]
        if requirement["status"] not in {"IN_PROGRESS", "IMPLEMENTED", "VALIDATED"}:
            raise ControlError(f"requirement {requirement_id} is not satisfied")
        requirement["status"] = "VALIDATED"
        if evidence_ref not in requirement["validation_evidence"]:
            requirement["validation_evidence"].append(evidence_ref)

    registry_path = root / ".nxs/phase-registry.json"
    registry = load_json(registry_path)
    phase = indexed(cast(list[dict[str, Any]], registry["phases"]), "phase")[phase_id]
    phase["status"] = "READY"
    phase["decision"] = "GO"

    evidence_refs = [
        str(path.relative_to(root))
        for path in sorted((root / f".nxs/evidence/{phase_id}").glob("*.json"))
    ]
    manifest.update(
        {
            "status": "READY",
            "decision": "GO",
            "implementation_commit": implementation_commit,
            "evidence": evidence_refs,
        }
    )
    manifest["timestamps"]["closed_at"] = iso_now()

    state_path = root / ".nxs/project-state.json"
    state = load_json(state_path)
    state["current_phase"] = {
        "id": phase_id,
        "status": "READY",
        "decision": "GO",
        "branch": manifest["branch"],
        "implementation_commit": implementation_commit,
        "closure_commit": None,
    }
    state["completed_phases"] = sorted(set(state["completed_phases"] + [phase_id]))
    state["active_phase"] = None
    state["blocked_phases"] = [b for b in state["blocked_phases"] if b != phase_id]
    state["next_allowed_execution"] = next_allowed_execution(
        root, registry_list=cast(list[dict[str, Any]], registry["phases"]), state=state
    )
    state["last_updated_at"] = iso_now()

    readiness_path = root / ".nxs/readiness.json"
    readiness = load_json(readiness_path)
    test_evidence = load_json(root / f".nxs/evidence/{phase_id}/tests.json")
    retained = [entry for entry in readiness["phases"] if entry["phase"] != phase_id]
    readiness["phases"] = [
        *retained,
        {
            "phase": phase_id,
            "branch": manifest["branch"],
            "implementation_commit": implementation_commit,
            "closure_reference": None,
            "requirements": manifest["requirements_implemented"],
            "gates": matrix["gates"],
            "evidence": manifest["evidence"],
            "test_count": test_evidence["passed"],
            "security_status": "PASS",
            "regression_status": "PASS",
            "decision": "GO",
            "status": "READY",
            "timestamp": iso_now(),
        },
    ]

    write_json(requirements_path, requirements_doc)
    write_json(registry_path, registry)
    write_json(manifest_path, manifest)
    write_json(state_path, state)
    write_json(readiness_path, readiness)
    release_if_held(root, phase_id)
    validate_invariants(root)
    return {
        "phase": phase_id,
        "decision": "GO",
        "next_allowed_execution": state["next_allowed_execution"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True)
    parser.add_argument("--implementation-commit", required=True)
    arguments = parser.parse_args()
    try:
        result = close(arguments.phase, arguments.implementation_commit)
    except ControlError as exc:
        print(f"NXS CLOSURE\nRESULT: BLOCK\nREASON: {exc}")
        return 2
    print(
        "NXS CLOSURE\n"
        f"RESULT: PASS\nPHASE: {arguments.phase}\nDECISION: GO\n"
        f"NEXT ALLOWED PHASE: {result['next_allowed_execution']['phase']}\n"
        f"NEXT CONDITION: {result['next_allowed_execution']['condition']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
