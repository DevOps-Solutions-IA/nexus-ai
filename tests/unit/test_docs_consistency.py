"""Regression gates for README/infrastructure parity against NXS state."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scripts.nxs_control.core import ControlError
from scripts.nxs_docs.__main__ import END, START, synchronize

ROOT = Path(__file__).parents[2]


def _fixture(tmp_path: Path, *, status: str = "PLANNED", decision: str = "PENDING") -> Path:
    (tmp_path / ".nxs").mkdir()
    (tmp_path / "infrastructure").mkdir()
    for filename in ("phase-registry.json", "project-state.json"):
        shutil.copyfile(ROOT / ".nxs" / filename, tmp_path / ".nxs" / filename)
    for filename in ("README.md", "infrastructure/README.md"):
        shutil.copyfile(ROOT / filename, tmp_path / filename)
    registry_path = tmp_path / ".nxs/phase-registry.json"
    data = json.loads(registry_path.read_text())
    phase = next(phase for phase in data["phases"] if phase["id"] == "NXS-P22")
    phase.update(status=status, decision=decision)
    registry_path.write_text(json.dumps(data))
    synchronize(tmp_path, write=True)
    return tmp_path


def test_current_readmes_match_canonical_revision() -> None:
    synchronize(ROOT)


@pytest.mark.parametrize(
    ("status", "decision", "changed_status", "changed_decision"),
    [
        ("PLANNED", "PENDING", "BUILDING", "PENDING"),
        ("BUILDING", "PENDING", "VALIDATING", "PENDING"),
        ("VALIDATING", "PENDING", "READY", "GO"),
        ("READY", "GO", "PLANNED", "PENDING"),
    ],
)
@pytest.mark.parametrize("filename", ["README.md", "infrastructure/README.md"])
def test_phase_status_drift_blocks_until_both_docs_regenerated(
    tmp_path: Path,
    status: str,
    decision: str,
    changed_status: str,
    changed_decision: str,
    filename: str,
) -> None:
    root = _fixture(tmp_path, status=status, decision=decision)
    synchronize(root)
    stale_doc = (root / filename).read_text()
    baseline_row = next(
        row for row in stale_doc.splitlines() if row.startswith("|") and "`NXS-P22`" in row
    )
    assert f"`{status} / {decision}`" in baseline_row
    registry_path = root / ".nxs/phase-registry.json"
    data = json.loads(registry_path.read_text())
    phase = next(p for p in data["phases"] if p["id"] == "NXS-P22")
    phase.update(status=changed_status, decision=changed_decision)
    registry_path.write_text(json.dumps(data))
    with pytest.raises(ControlError, match="stale status"):
        synchronize(root)
    synchronize(root, write=True)
    synchronize(root)
    for document in ("README.md", "infrastructure/README.md"):
        phase_row = next(
            row
            for row in (root / document).read_text().splitlines()
            if row.startswith("|") and "`NXS-P22`" in row
        )
        assert f"`{changed_status} / {changed_decision}`" in phase_row
    (root / filename).write_text(stale_doc)
    with pytest.raises(ControlError, match=f"{filename}: stale status"):
        synchronize(root)
    synchronize(root, write=True)
    synchronize(root)


@pytest.mark.parametrize("filename", ["README.md", "infrastructure/README.md"])
def test_status_marker_cannot_be_deleted(tmp_path: Path, filename: str) -> None:
    root = _fixture(tmp_path)
    doc = root / filename
    doc.write_text(doc.read_text().replace(START, "").replace(END, ""))
    with pytest.raises(ControlError, match="expected one documentation status block"):
        synchronize(root)


@pytest.mark.parametrize("filename", ["README.md", "infrastructure/README.md"])
def test_manual_falsification_fails_closed(tmp_path: Path, filename: str) -> None:
    root = _fixture(tmp_path)
    path = root / filename
    original = path.read_text()
    phase_row = next(
        row for row in original.splitlines() if row.startswith("|") and "`NXS-P22`" in row
    )
    path.write_text(
        original.replace(phase_row, phase_row.replace("`PLANNED / PENDING`", "`READY / GO`"))
    )
    with pytest.raises(ControlError, match="stale status"):
        synchronize(root)


def test_documentation_gate_remains_mandatory_in_ci() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    validator = (ROOT / "scripts/nxs_validate/__main__.py").read_text()
    agents = (ROOT / "AGENTS.md").read_text()
    assert "Documentation parity (mandatory)" in ci
    assert "uv run python -m scripts.nxs_docs" in ci
    assert "synchronize(root)" in validator
    assert "fail-closed" in agents
