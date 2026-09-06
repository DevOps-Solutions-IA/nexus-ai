from __future__ import annotations

import argparse
import sys
from typing import Any, cast

from scripts.nxs_control.core import (
    ControlError,
    indexed,
    iso_now,
    load_json,
    repository_root,
    transition_allowed,
    validate_invariants,
    write_json,
)


def transition(phase_id: str, target: str) -> None:
    root = repository_root()
    validate_invariants(root)
    registry_path = root / ".nxs/phase-registry.json"
    registry = load_json(registry_path)
    phases = indexed(cast(list[dict[str, Any]], registry["phases"]), "phase")
    if phase_id not in phases:
        raise ControlError(f"unknown phase {phase_id}")
    phase = phases[phase_id]
    current = cast(str, phase["status"])
    if not transition_allowed(current, target):
        raise ControlError(f"invalid transition {current} -> {target}")
    decision = (
        "GO" if target == "READY" else "NO_GO" if target in {"FAILED", "BLOCKED"} else "PENDING"
    )
    phase["status"] = target
    phase["decision"] = decision
    manifest_path = root / f".nxs/phases/{phase_id}.json"
    manifest = load_json(manifest_path)
    manifest["status"] = target
    manifest["decision"] = decision
    state_path = root / ".nxs/project-state.json"
    state = load_json(state_path)
    if state["current_phase"]["id"] == phase_id:
        state["current_phase"]["status"] = target
        state["current_phase"]["decision"] = decision
        state["last_updated_at"] = iso_now()
    write_json(registry_path, registry)
    write_json(manifest_path, manifest)
    write_json(state_path, state)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase")
    parser.add_argument("target")
    arguments = parser.parse_args()
    try:
        transition(arguments.phase, arguments.target)
        validate_invariants(repository_root())
    except ControlError as exc:
        print(f"NXS STATE TRANSITION: REJECTED\nREASON: {exc}")
        return 2
    print(f"NXS STATE TRANSITION: ACCEPTED\nPHASE: {arguments.phase}\nSTATUS: {arguments.target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
