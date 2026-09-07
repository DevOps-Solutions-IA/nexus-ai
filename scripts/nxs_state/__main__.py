from __future__ import annotations

import argparse
import sys
from pathlib import Path
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


def transition(phase_id: str, target: str, *, root: Path | None = None) -> None:
    root = root or repository_root()
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
    manifest = load_json(manifest_path) if manifest_path.exists() else None
    if manifest is not None:
        manifest["status"] = target
        manifest["decision"] = decision
    state_path = root / ".nxs/project-state.json"
    state = load_json(state_path)
    if state["current_phase"]["id"] == phase_id:
        state["current_phase"]["status"] = target
        state["current_phase"]["decision"] = decision
        state["last_updated_at"] = iso_now()
    if target == "BLOCKED" and phase_id not in state["blocked_phases"]:
        state["blocked_phases"] = sorted({*state["blocked_phases"], phase_id})
    if target != "BLOCKED":
        state["blocked_phases"] = [b for b in state["blocked_phases"] if b != phase_id]
    write_json(registry_path, registry)
    if manifest is not None:
        write_json(manifest_path, manifest)
    write_json(state_path, state)
    validate_invariants(root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase")
    parser.add_argument("target")
    arguments = parser.parse_args()
    try:
        transition(arguments.phase, arguments.target)
    except ControlError as exc:
        print(f"NXS STATE TRANSITION: REJECTED\nREASON: {exc}")
        return 2
    print(f"NXS STATE TRANSITION: ACCEPTED\nPHASE: {arguments.phase}\nSTATUS: {arguments.target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
