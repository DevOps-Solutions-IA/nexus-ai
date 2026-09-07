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


def run_gate(root: Path, phase: str) -> dict[str, object]:
    gates: dict[str, str] = {}
    details: dict[str, dict[str, object]] = {}
    for name, command in COMMANDS:
        completed = subprocess.run(
            command, cwd=root, check=False, capture_output=True, text=True, timeout=300
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
