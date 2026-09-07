from __future__ import annotations

import argparse
import json
import os
import sys

from scripts.nxs_control.core import (
    ControlError,
    evaluate_guard,
    phase_order,
    registry_phases,
    repository_root,
)
from scripts.nxs_guard import lock as lock_module


def _ci_branch(explicit: str | None) -> str | None:
    return explicit or os.getenv("GITHUB_HEAD_REF") or os.getenv("GITHUB_REF_NAME") or None


def _phase_for_branch(branch: str) -> str | None:
    root = repository_root()
    for phase_id, phase in registry_phases(root).items():
        if phase["branch"] == branch:
            return phase_id
    return None


def run_guard(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="nxs_guard", description="NXS execution guard")
    parser.add_argument("--phase")
    parser.add_argument("--branch")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--infer-phase",
        action="store_true",
        help="resolve the phase from the current or CI branch instead of --phase",
    )
    arguments = parser.parse_args(argv)
    branch = _ci_branch(arguments.branch)
    phase = arguments.phase
    if arguments.infer_phase or phase is None:
        resolved = _phase_for_branch(branch) if branch else None
        if resolved is None:
            from scripts.nxs_control.core import git

            resolved = _phase_for_branch(git(repository_root(), "branch", "--show-current"))
        if resolved is None:
            print("NXS EXECUTION GUARD\nRESULT: BLOCK\nREASON: cannot resolve phase from branch")
            return 2
        phase = resolved
    result = evaluate_guard(repository_root(), phase, branch)
    if arguments.json:
        print(json.dumps(result.as_dict(), sort_keys=True))
    else:
        print("NXS EXECUTION GUARD")
        print(f"RESULT: {result.result}")
        print(f"CODE: {result.code}")
        for reason in result.reasons:
            print(f"REASON: {reason}")
    return 0 if result.result == "PASS" else 2


def run_lock(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="nxs_guard lock", description="NXS execution lock")
    parser.add_argument("operation", choices=("acquire", "status", "release", "recover"))
    parser.add_argument("--phase")
    parser.add_argument("--actor")
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args(argv)
    root = repository_root()
    try:
        if arguments.operation == "acquire":
            if not arguments.phase or not arguments.actor:
                raise ControlError("acquire requires --phase and --actor")
            lock_module.acquire(root, arguments.phase, arguments.actor)
        elif arguments.operation == "release":
            if not arguments.phase:
                raise ControlError("release requires --phase")
            lock_module.release(root, arguments.phase)
        elif arguments.operation == "recover":
            lock_module.recover(root)
    except ControlError as exc:
        print(f"NXS EXECUTION LOCK\nRESULT: REJECTED\nREASON: {exc}")
        return 2
    state = lock_module.status(root)
    if arguments.json:
        print(json.dumps(state, sort_keys=True))
    else:
        print("NXS EXECUTION LOCK")
        print(f"OPERATION: {arguments.operation}")
        print(f"STATE: {state['state']}")
        if state["state"] == "ACTIVE":
            print(f"PHASE: {state['phase']}")
            print(f"ACTOR: {state['actor']}")
            print(f"STALE: {str(state['stale']).lower()}")
            print(f"SECONDS_REMAINING: {state['seconds_remaining']}")
    return 0


def run_current_phase() -> int:
    from scripts.nxs_control.core import git

    root = repository_root()
    branch = _ci_branch(None) or git(root, "branch", "--show-current")
    resolved = _phase_for_branch(branch)
    if resolved is None:
        return 1
    print(resolved)
    return 0


def run_phase_order() -> int:
    for phase_id in phase_order(repository_root()):
        print(phase_id)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "lock":
        return run_lock(args[1:])
    if args and args[0] == "current-phase":
        return run_current_phase()
    if args and args[0] == "phase-order":
        return run_phase_order()
    return run_guard(args)


if __name__ == "__main__":
    sys.exit(main())
