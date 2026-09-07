from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

from scripts.nxs_control.core import (
    ControlError,
    evaluate_guard,
    indexed,
    iso_now,
    load_json,
    mandatory_requirements_for,
    registry_phases,
    repository_root,
    transition_allowed,
    validate_invariants,
    write_json,
)
from scripts.nxs_guard.lock import acquire

_DEFAULT_GATES: tuple[str, ...] = (
    "repository_integrity",
    "formatting",
    "lint",
    "typing",
    "unit_tests",
    "integration_tests",
    "contract_tests",
    "security_tests",
    "concurrency_tests",
    "resilience_tests",
    "coverage",
    "sast",
    "secret_scan",
    "dependency_security",
    "container_scan",
    "docker_build",
    "non_root_runtime",
    "graceful_shutdown",
    "migration_validation",
    "execution_guard",
    "generic_phase_start",
    "generic_closure",
    "generic_next_phase",
    "execution_lock",
    "phase_neutral_ci",
    "toolchain_consistency",
    "agent_handoff",
    "clean_room",
    "documentation",
    "regression",
    "github_ci",
)

_LIFECYCLE_TO_BUILDING: dict[str, tuple[str, ...]] = {
    "PLANNED": ("READY_TO_EXECUTE", "BUILDING"),
    "FAILED": ("READY_TO_EXECUTE", "BUILDING"),
    "BLOCKED": ("READY_TO_EXECUTE", "BUILDING"),
    "READY_TO_EXECUTE": ("BUILDING",),
}


def _ensure_manifest(root: Path, phase_id: str, phase: dict[str, Any]) -> Path:
    manifest_path = root / f".nxs/phases/{phase_id}.json"
    if manifest_path.exists():
        return manifest_path
    requirements = mandatory_requirements_for(root, phase_id)
    if not requirements:
        raise ControlError(
            f"cannot auto-create a manifest for {phase_id}: no mandatory requirements target it"
        )
    manifest = {
        "$schema": "../phase-manifest.schema.json",
        "schema_version": "1.0.0",
        "id": phase_id,
        "title": phase["title"],
        "objective": (
            f"Deliver the {phase['title']} phase per its branch contract and requirements."
        ),
        "branch": phase["branch"],
        "dependencies": list(phase["dependencies"]),
        "requirements_implemented": requirements,
        "non_scope": ["Defined in the phase branch contract"],
        "mandatory_gates": list(_DEFAULT_GATES),
        "status": phase["status"],
        "decision": phase["decision"],
        "evidence": [],
        "implementation_commit": None,
        "closure_commit": None,
        "timestamps": {"started_at": None, "closed_at": None},
    }
    write_json(manifest_path, manifest)
    return manifest_path


def start(phase_id: str, actor: str, *, root: Path | None = None) -> dict[str, Any]:
    root = root or repository_root()
    validate_invariants(root)

    phases = registry_phases(root)
    if phase_id not in phases:
        raise ControlError(f"unknown phase {phase_id}")
    phase = phases[phase_id]

    state_path = root / ".nxs/project-state.json"
    state = load_json(state_path)
    if (
        state["active_phase"] == phase_id
        and phase["status"] == "BUILDING"
        and state["current_phase"]["id"] == phase_id
    ):
        return {"phase": phase_id, "status": "BUILDING", "note": "already active"}

    guard = evaluate_guard(root, phase_id)
    if guard.result != "PASS":
        raise ControlError(f"guard blocks start ({guard.code}): " + "; ".join(guard.reasons))

    if not actor.strip():
        raise ControlError("an actor is required to start a phase")

    manifest_path = _ensure_manifest(root, phase_id, phase)
    manifest = load_json(manifest_path)

    expected = set(mandatory_requirements_for(root, phase_id))
    mapped = set(cast(list[str], manifest["requirements_implemented"]))
    if mapped != expected:
        raise ControlError(
            f"{phase_id} manifest requirement mapping mismatch: "
            f"expected {sorted(expected)}, got {sorted(mapped)}"
        )

    steps = _LIFECYCLE_TO_BUILDING.get(cast(str, phase["status"]))
    if steps is None:
        raise ControlError(f"{phase_id} status {phase['status']} cannot transition to BUILDING")
    current_status = cast(str, phase["status"])
    for target in steps:
        if not transition_allowed(current_status, target):
            raise ControlError(f"illegal transition {current_status} -> {target}")
        current_status = target

    requirements_path = root / ".nxs/requirements.json"
    requirements_doc = load_json(requirements_path)
    requirements = indexed(
        cast(list[dict[str, Any]], requirements_doc["requirements"]), "requirement"
    )
    for requirement_id in sorted(expected):
        if requirements[requirement_id]["status"] == "PLANNED":
            requirements[requirement_id]["status"] = "IN_PROGRESS"

    registry_path = root / ".nxs/phase-registry.json"
    registry = load_json(registry_path)
    indexed(cast(list[dict[str, Any]], registry["phases"]), "phase")[phase_id].update(
        {"status": "BUILDING", "decision": "PENDING"}
    )

    manifest.update({"status": "BUILDING", "decision": "PENDING"})
    manifest["timestamps"]["started_at"] = iso_now()

    state["current_phase"] = {
        "id": phase_id,
        "status": "BUILDING",
        "decision": "PENDING",
        "branch": phase["branch"],
        "implementation_commit": None,
        "closure_commit": None,
    }
    state["active_phase"] = phase_id
    state["blocked_phases"] = [b for b in state["blocked_phases"] if b != phase_id]
    state["next_allowed_execution"] = {"phase": phase_id, "condition": "ACTIVE_PHASE_ONLY"}
    state["last_updated_at"] = iso_now()

    write_json(requirements_path, requirements_doc)
    write_json(registry_path, registry)
    write_json(manifest_path, manifest)
    write_json(state_path, state)

    lock = acquire(root, phase_id, actor)
    validate_invariants(root)
    return {
        "phase": phase_id,
        "status": "BUILDING",
        "branch": phase["branch"],
        "actor": actor,
        "requirements_in_progress": sorted(expected),
        "lock_expires_at": lock["expires_at"],
        "started_at": manifest["timestamps"]["started_at"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="nxs_start", description="Start an eligible NXS phase")
    parser.add_argument("--phase", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args()
    try:
        result = start(arguments.phase, arguments.actor)
    except ControlError as exc:
        print(f"NXS PHASE START\nRESULT: BLOCK\nREASON: {exc}")
        return 2
    if arguments.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print("NXS PHASE START")
        print("RESULT: PASS")
        for key, value in result.items():
            print(f"{key.upper()}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
