from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from scripts.nxs_control.core import repository_root

COMMANDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("repository_integrity", ("uv", "run", "python", "-m", "scripts.nxs_validate")),
    ("formatting", ("uv", "run", "ruff", "format", "--check", ".")),
    ("lint", ("uv", "run", "ruff", "check", ".")),
    ("typing", ("uv", "run", "mypy")),
    ("tests", ("uv", "run", "pytest", "-q")),
    ("sast", ("uv", "run", "bandit", "-q", "-lll", "-c", "pyproject.toml", "-r", "src", "scripts")),
    ("dependency_security", ("uv", "run", "pip-audit")),
)


#: the gate's own subprocess ceiling per command — NOT a test-suite budget. Raised
#: from 300s (audit corrective #10) after the P13 full-suite pytest run's organic,
#: legitimate growth across 10 correctives (1478 -> 1553 tests) pushed its own
#: wall-clock time into the 286-306s range on ordinary shared-machine load, making the
#: old ceiling a tooling false-positive rather than a real hang/deadlock signal.
#: Doesn't skip, weaken, or lower any check or threshold — every command below still
#: must exit 0 to pass; this only gives the slowest of them (tests) realistic room to
#: finish and report its actual result.
_COMMAND_TIMEOUT_SECONDS = 900


def run_gate(root: Path, phase: str) -> dict[str, object]:
    gates: dict[str, str] = {}
    details: dict[str, dict[str, object]] = {}
    for name, command in COMMANDS:
        completed = subprocess.run(
            command,
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
        gates[name] = "PASS" if completed.returncode == 0 else "FAIL"
        details[name] = {
            "command": list(command),
            "exit_code": completed.returncode,
            "output_tail": (completed.stdout + completed.stderr)[-2000:],
        }
    result = "PASS" if all(value == "PASS" for value in gates.values()) else "FAIL"
    evidence: dict[str, object] = {
        "schema_version": "1.0.0",
        "phase": phase,
        "result": result,
        "decision_eligibility": "GO" if result == "PASS" else "NO_GO",
        "executed_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "gates": gates,
        "details": details,
    }
    evidence_path = root / f".nxs/evidence/{phase}/quality-gates.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True)
    arguments = parser.parse_args()
    evidence = run_gate(repository_root(), arguments.phase)
    print("NXS QUALITY GATE")
    print(f"RESULT: {evidence['result']}")
    print(f"GO/NO-GO ELIGIBILITY: {evidence['decision_eligibility']}")
    return 0 if evidence["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
