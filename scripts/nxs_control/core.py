from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

from jsonschema import Draft202012Validator, FormatChecker

PHASE_STATUSES: Final = {
    "PLANNED",
    "READY_TO_EXECUTE",
    "BUILDING",
    "VALIDATING",
    "FAILED",
    "BLOCKED",
    "READY",
}
TRANSITIONS: Final = {
    "PLANNED": {"READY_TO_EXECUTE", "BLOCKED"},
    "READY_TO_EXECUTE": {"BUILDING", "BLOCKED"},
    "BUILDING": {"VALIDATING", "FAILED", "BLOCKED"},
    "VALIDATING": {"READY", "FAILED", "BLOCKED"},
    "FAILED": {"READY_TO_EXECUTE", "BLOCKED"},
    "BLOCKED": {"READY_TO_EXECUTE"},
    # READY is terminal for normal execution. A closed phase may be reopened to
    # VALIDATING only for a corrective delta (e.g. an audit finding); the reopen path
    # clears the phase's READY/GO closure state and makes it the active phase again.
    "READY": {"VALIDATING"},
}
REOPEN_TRANSITION: Final = ("READY", "VALIDATING")


class ControlError(RuntimeError):
    """Raised when an NXS invariant is violated."""


@dataclass(frozen=True)
class GuardResult:
    result: str
    code: str
    reasons: tuple[str, ...]
    phase: str

    def as_dict(self) -> dict[str, object]:
        return {
            "system": "NXS EXECUTION GUARD",
            "result": self.result,
            "code": self.code,
            "phase": self.phase,
            "reasons": list(self.reasons),
        }


def repository_root(start: Path | None = None) -> Path:
    candidate = (start or Path.cwd()).resolve()
    for directory in (candidate, *candidate.parents):
        if (directory / ".git").exists() and (directory / ".nxs").is_dir():
            return directory
    raise ControlError("repository root containing .git and .nxs was not found")


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlError(f"cannot read valid JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ControlError(f"expected JSON object in {path}")
    return cast(dict[str, Any], value)


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso_now() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, check=False, capture_output=True, text=True, timeout=30
    )
    if completed.returncode != 0:
        raise ControlError(completed.stderr.strip() or f"git {' '.join(args)} failed")
    return completed.stdout.strip()


def validate_document(document: Path, schema: Path) -> None:
    validator = Draft202012Validator(load_json(schema), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(load_json(document)), key=lambda item: list(item.path))
    if errors:
        messages = [
            f"{'.'.join(map(str, error.path)) or '<root>'}: {error.message}" for error in errors
        ]
        raise ControlError(f"schema validation failed for {document}: {'; '.join(messages)}")


def validate_all_schemas(root: Path) -> None:
    nxs = root / ".nxs"
    pairs = [
        ("project-state.json", "project-state.schema.json"),
        ("execution-policy.json", "execution-policy.schema.json"),
        ("requirements.json", "requirements.schema.json"),
        ("phase-registry.json", "phase-registry.schema.json"),
        ("readiness.json", "readiness.schema.json"),
        ("execution-lock.json", "execution-lock.schema.json"),
    ]
    for document, schema in pairs:
        validate_document(nxs / document, nxs / schema)
    for manifest in sorted((nxs / "phases").glob("NXS-P*.json")):
        validate_document(manifest, nxs / "phase-manifest.schema.json")


def indexed(items: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        identifier = cast(str, item["id"])
        if identifier in result:
            raise ControlError(f"duplicate {label} identifier: {identifier}")
        result[identifier] = item
    return result


def validate_invariants(root: Path) -> None:
    validate_all_schemas(root)
    nxs = root / ".nxs"
    state = load_json(nxs / "project-state.json")
    phases = indexed(
        cast(list[dict[str, Any]], load_json(nxs / "phase-registry.json")["phases"]), "phase"
    )
    requirements = indexed(
        cast(list[dict[str, Any]], load_json(nxs / "requirements.json")["requirements"]),
        "requirement",
    )
    readiness = cast(list[dict[str, Any]], load_json(nxs / "readiness.json")["phases"])
    readiness_by_phase = {cast(str, entry["phase"]): entry for entry in readiness}
    if len(readiness_by_phase) != len(readiness):
        raise ControlError("duplicate readiness phase")
    for phase_id, phase in phases.items():
        for dependency in cast(list[str], phase["dependencies"]):
            if dependency not in phases:
                raise ControlError(f"{phase_id} references unknown dependency {dependency}")
        if phase["status"] == "READY":
            if phase["decision"] != "GO":
                raise ControlError(f"{phase_id} READY requires GO")
            entry = readiness_by_phase.get(phase_id)
            if entry is None or any(value != "PASS" for value in entry["gates"].values()):
                raise ControlError(
                    f"{phase_id} READY requires readiness evidence with all gates PASS"
                )
            for dependency in cast(list[str], phase["dependencies"]):
                if (
                    phases[dependency]["status"] != "READY"
                    or phases[dependency]["decision"] != "GO"
                ):
                    raise ControlError(
                        f"{phase_id} READY requires dependency {dependency} READY/GO"
                    )
        if phase["status"] in {"FAILED", "BLOCKED"} and phase["decision"] == "GO":
            raise ControlError(f"{phase_id} {phase['status']} cannot have GO")
    for requirement_id, requirement in requirements.items():
        if requirement["target_phase"] not in phases:
            raise ControlError(f"{requirement_id} targets unknown phase")
        for dependency in cast(list[str], requirement["dependencies"]):
            if dependency not in requirements:
                raise ControlError(f"{requirement_id} references unknown requirement {dependency}")
    manifests = {path.stem: load_json(path) for path in (nxs / "phases").glob("NXS-P*.json")}
    for phase_id, manifest in manifests.items():
        registry_phase = phases.get(phase_id)
        if registry_phase is None:
            raise ControlError(f"manifest {phase_id} is absent from registry")
        if (
            manifest["status"] != registry_phase["status"]
            or manifest["decision"] != registry_phase["decision"]
        ):
            raise ControlError(f"manifest and registry disagree for {phase_id}")
        mapped = set(cast(list[str], manifest["requirements_implemented"]))
        expected = {
            identifier
            for identifier, item in requirements.items()
            if item["target_phase"] == phase_id and item["mandatory"]
        }
        if mapped != expected:
            raise ControlError(
                f"{phase_id} mandatory requirement mapping mismatch: "
                f"expected {sorted(expected)}, got {sorted(mapped)}"
            )
    current = cast(dict[str, Any], state["current_phase"])
    if current["id"] not in phases or current["status"] != phases[current["id"]]["status"]:
        raise ControlError("project state current phase disagrees with phase registry")
    if current["decision"] != phases[current["id"]]["decision"]:
        raise ControlError("project state decision disagrees with phase registry")
    completed = set(cast(list[str], state["completed_phases"]))
    actual_completed = {
        identifier for identifier, phase in phases.items() if phase["status"] == "READY"
    }
    if completed != actual_completed:
        raise ControlError("completed_phases must exactly match READY phases")


def transition_allowed(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, set())


def evaluate_guard(root: Path, phase_id: str, branch: str | None = None) -> GuardResult:
    try:
        validate_invariants(root)
        nxs = root / ".nxs"
        registry = indexed(
            cast(list[dict[str, Any]], load_json(nxs / "phase-registry.json")["phases"]), "phase"
        )
        if phase_id not in registry:
            raise ControlError(f"unknown phase {phase_id}")
        phase = registry[phase_id]
        if phase["status"] == "READY":
            return GuardResult(
                "BLOCK",
                "ALREADY_READY",
                ("phase is already READY/GO; a documented delta is required",),
                phase_id,
            )
        actual_branch = branch if branch is not None else git(root, "branch", "--show-current")
        reasons: list[str] = []
        if actual_branch != phase["branch"]:
            reasons.append(f"branch {actual_branch!r} does not match required {phase['branch']!r}")
        for dependency_id in cast(list[str], phase["dependencies"]):
            dependency = registry[dependency_id]
            if dependency["status"] != "READY" or dependency["decision"] != "GO":
                reasons.append(f"dependency {dependency_id} is not READY/GO")
        state = load_json(nxs / "project-state.json")
        active_phase = state["active_phase"]
        if active_phase not in (None, phase_id):
            reasons.append(f"conflicting active phase {active_phase}")
        lock = load_json(nxs / "execution-lock.json")
        if lock["state"] == "ACTIVE" and lock["phase"] != phase_id:
            reasons.append(f"execution lock is held by {lock['phase']}")
        if lock["state"] == "ACTIVE" and lock["phase"] == phase_id:
            expires_at = datetime.fromisoformat(
                cast(str, lock["expires_at"]).replace("Z", "+00:00")
            )
            if expires_at <= utc_now():
                reasons.append("execution lock is stale; run controlled lock recovery")
        if reasons:
            return GuardResult("BLOCK", "PRECONDITION_FAILED", tuple(reasons), phase_id)
        return GuardResult("PASS", "AUTHORIZED", (), phase_id)
    except ControlError as exc:
        return GuardResult("BLOCK", "MALFORMED_STATE", (str(exc),), phase_id)


def registry_phase_list(root: Path) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], load_json(root / ".nxs/phase-registry.json")["phases"])


def registry_phases(root: Path) -> dict[str, dict[str, Any]]:
    return indexed(registry_phase_list(root), "phase")


def phase_order(root: Path) -> list[str]:
    return [cast(str, phase["id"]) for phase in registry_phase_list(root)]


def dependencies_ready(phases: dict[str, dict[str, Any]], phase: dict[str, Any]) -> bool:
    return all(
        phases[dependency]["status"] == "READY" and phases[dependency]["decision"] == "GO"
        for dependency in cast(list[str], phase["dependencies"])
    )


def eligible_phases(
    root: Path,
    *,
    registry_list: list[dict[str, Any]] | None = None,
    state: dict[str, Any] | None = None,
) -> list[str]:
    """Phases that may legally be started next, in deterministic registry order.

    A phase is eligible only when it is not already READY, is not blocked, has every
    dependency at READY/GO, and does not conflict with a different active phase. When
    multiple phases qualify, registry order is the documented deterministic policy.

    ``registry_list`` and ``state`` may be supplied to evaluate against in-memory data
    that has not yet been persisted (used by the closure engine).
    """
    registry_list = registry_list if registry_list is not None else registry_phase_list(root)
    phases = indexed(registry_list, "phase")
    state = state if state is not None else load_json(root / ".nxs/project-state.json")
    active = state["active_phase"]
    blocked = set(cast(list[str], state.get("blocked_phases", [])))
    result: list[str] = []
    for phase in registry_list:
        phase_id = cast(str, phase["id"])
        if phase["status"] == "READY" or phase["status"] == "BLOCKED" or phase_id in blocked:
            continue
        if not dependencies_ready(phases, phase):
            continue
        if active not in (None, phase_id):
            continue
        result.append(phase_id)
    return result


def next_eligible_phase(
    root: Path,
    *,
    registry_list: list[dict[str, Any]] | None = None,
    state: dict[str, Any] | None = None,
) -> str | None:
    candidates = eligible_phases(root, registry_list=registry_list, state=state)
    return candidates[0] if candidates else None


def next_allowed_execution(
    root: Path,
    *,
    registry_list: list[dict[str, Any]] | None = None,
    state: dict[str, Any] | None = None,
) -> dict[str, str | None]:
    """Compute next_allowed_execution generically from active phase and the registry DAG."""
    state = state if state is not None else load_json(root / ".nxs/project-state.json")
    active = state["active_phase"]
    if active is not None:
        return {"phase": cast(str, active), "condition": "ACTIVE_PHASE_ONLY"}
    candidate = next_eligible_phase(root, registry_list=registry_list, state=state)
    if candidate is None:
        return {"phase": None, "condition": "NONE"}
    return {"phase": candidate, "condition": "DEPENDENCIES_READY"}


def commit_exists(root: Path, sha: str) -> bool:
    try:
        git(root, "cat-file", "-e", f"{sha}^{{commit}}")
    except ControlError:
        return False
    return True


def mandatory_requirements_for(root: Path, phase_id: str) -> list[str]:
    requirements = cast(
        list[dict[str, Any]], load_json(root / ".nxs/requirements.json")["requirements"]
    )
    return sorted(
        cast(str, item["id"])
        for item in requirements
        if item["target_phase"] == phase_id and item["mandatory"]
    )
