from __future__ import annotations

from itertools import pairwise
from pathlib import Path
from typing import Any

from scripts.nxs_control.core import ControlError, commit_exists, indexed, load_json

TRANSITIONS = {
    "OPEN": {"IN_PROGRESS", "RESOLVED"},
    "IN_PROGRESS": {"OPEN", "RESOLVED"},
    "RESOLVED": {"OPEN"},
}


def dependency_closure(phases: dict[str, dict[str, Any]], phase_id: str) -> set[str]:
    visited: set[str] = set()

    def visit(identifier: str, ancestors: set[str]) -> None:
        if identifier in ancestors:
            raise ControlError(f"phase dependency cycle at {identifier}")
        for dependency in phases[identifier]["dependencies"]:
            if dependency not in phases:
                raise ControlError(f"unknown phase dependency {dependency}")
            if dependency not in visited:
                visit(dependency, ancestors | {identifier})
                visited.add(dependency)

    visit(phase_id, set())
    return visited


def validate_obligations(root: Path, phases: dict[str, dict[str, Any]]) -> None:
    document = load_json(root / ".nxs/deferred-obligations.json")
    obligations = indexed(document["obligations"], "obligation")
    policies = indexed(document["certification_policies"], "certification policy")
    closures = {phase_id: dependency_closure(phases, phase_id) for phase_id in phases}
    for policy_id, policy in policies.items():
        if policy_id not in phases:
            raise ControlError(f"unknown certification phase {policy_id}")
        for dependency in policy["required_ancestors"]:
            if dependency not in phases or dependency not in closures[policy_id]:
                raise ControlError(f"{policy_id} requires ancestor {dependency}")
    for identifier, obligation in obligations.items():
        referenced = [obligation["source_phase"], obligation["owner_phase"]]
        referenced.extend(obligation["blocks_phases"])
        for phase_id in referenced:
            if phase_id not in phases:
                raise ControlError(f"{identifier} references unknown phase {phase_id}")
        history = obligation["status_history"]
        if history[0] != "OPEN" or history[-1] != obligation["status"]:
            raise ControlError(f"{identifier} status history does not match current status")
        if any(after not in TRANSITIONS[before] for before, after in pairwise(history)):
            raise ControlError(f"{identifier} has illegal obligation transition")
        if obligation["certification_impact"] == "PRODUCTION_BLOCKING":
            if not policies:
                raise ControlError(f"{identifier} requires a production certification policy")
            for policy_id in policies:
                if policy_id not in obligation["blocks_phases"]:
                    raise ControlError(f"{identifier} must block certification phase {policy_id}")
                if obligation["owner_phase"] not in closures[policy_id]:
                    raise ControlError(f"{identifier} owner must precede certification {policy_id}")
        if obligation["status"] == "RESOLVED":
            owner_id = obligation["owner_phase"]
            owner = phases[owner_id]
            if owner["status"] != "READY" or owner["decision"] != "GO":
                raise ControlError(f"{identifier} resolution requires owner {owner_id} READY/GO")
            manifest = load_json(root / f".nxs/phases/{owner_id}.json")
            resolution = obligation["resolution"]
            readiness = load_json(root / ".nxs/readiness.json")
            completion = next(entry for entry in readiness["phases"] if entry["phase"] == owner_id)
            if (
                manifest["status"] != "READY"
                or manifest["decision"] != "GO"
                or manifest["implementation_commit"] != resolution["implementation_commit"]
                or not commit_exists(root, resolution["implementation_commit"])
                or completion["implementation_commit"] != resolution["implementation_commit"]
            ):
                raise ControlError(f"{identifier} resolution must bind the owning implementation")
            for reference in resolution["evidence"]:
                evidence = root / reference
                if (
                    reference not in manifest["evidence"]
                    or not evidence.resolve().is_relative_to(root.resolve())
                    or not evidence.is_file()
                ):
                    raise ControlError(f"{identifier} resolution evidence is missing: {reference}")
        for blocked_phase in obligation["blocks_phases"]:
            if obligation["status"] != "RESOLVED" and phases[blocked_phase]["status"] == "READY":
                raise ControlError(f"{blocked_phase} READY blocked by unresolved {identifier}")


def unresolved_blockers(root: Path, phase_id: str) -> list[str]:
    document = load_json(root / ".nxs/deferred-obligations.json")
    return sorted(
        obligation["id"]
        for obligation in document["obligations"]
        if phase_id in obligation["blocks_phases"] and obligation["status"] != "RESOLVED"
    )


def require_closure_obligations(root: Path, phase_id: str) -> None:
    blockers = unresolved_blockers(root, phase_id)
    if blockers:
        raise ControlError(f"{phase_id} closure blocked by unresolved {', '.join(blockers)}")
