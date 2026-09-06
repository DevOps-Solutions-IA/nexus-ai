from __future__ import annotations

import json
import sys
from pathlib import Path

from scripts.nxs_control.core import ControlError, repository_root, validate_invariants

REQUIRED = (
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "pyproject.toml",
    "uv.lock",
    "Dockerfile",
    "compose.yaml",
    ".nxs/project-state.json",
    ".nxs/requirements.json",
    ".nxs/phase-registry.json",
    ".nxs/execution-policy.json",
    ".github/workflows/ci.yml",
)


def validate_files(root: Path) -> None:
    missing = [path for path in REQUIRED if not (root / path).exists()]
    if missing:
        raise ControlError(f"required files missing: {', '.join(missing)}")
    forbidden = ("Genesis " + "Nexus AI", "Box Evolution " + "AI")
    for path in root.rglob("*"):
        if (
            not path.is_file()
            or ".git" in path.parts
            or path.suffix not in {".md", ".json", ".py", ".toml", ".yaml", ".yml"}
        ):
            continue
        text = path.read_text(encoding="utf-8")
        for name in forbidden:
            if name in text:
                raise ControlError(f"forbidden legacy name in {path.relative_to(root)}")
    agents = (root / "AGENTS.md").read_text(encoding="utf-8")
    if "Do not trust conversational memory" not in agents or "nxs-preflight" not in agents:
        raise ControlError("AGENTS.md does not declare canonical governance")


def main() -> int:
    try:
        root = repository_root()
        validate_files(root)
        validate_invariants(root)
    except ControlError as exc:
        print(json.dumps({"validator": "NXS_REPOSITORY", "result": "FAIL", "reason": str(exc)}))
        return 1
    print(json.dumps({"validator": "NXS_REPOSITORY", "result": "PASS"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
