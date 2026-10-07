"""Regression gates for README/infrastructure parity against NXS state."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scripts.nxs_control.core import ControlError
from scripts.nxs_docs.__main__ import END, START, synchronize

ROOT = Path(__file__).parents[2]


def _fixture(tmp_path: Path) -> Path:
    (tmp_path / ".nxs").mkdir()
    (tmp_path / "infrastructure").mkdir()
    for filename in ("phase-registry.json", "project-state.json"):
        shutil.copyfile(ROOT / ".nxs" / filename, tmp_path / ".nxs" / filename)
    for filename in ("README.md", "infrastructure/README.md"):
        shutil.copyfile(ROOT / filename, tmp_path / filename)
    return tmp_path


def test_current_readmes_match_canonical_revision() -> None:
    synchronize(ROOT)


def test_phase_status_drift_blocks_until_both_docs_regenerated(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    registry_path = root / ".nxs/phase-registry.json"
    data = json.loads(registry_path.read_text())
    phase = next(p for p in data["phases"] if p["id"] == "NXS-P22")
    assert phase["status"] == "PLANNED"
    phase["status"] = "BUILDING"
    registry_path.write_text(json.dumps(data))
    with pytest.raises(ControlError, match="stale status"):
        synchronize(root)
    synchronize(root, write=True)
    synchronize(root)
    assert "`BUILDING / PENDING`" in (root / "README.md").read_text()
    assert "`BUILDING / PENDING`" in (root / "infrastructure/README.md").read_text()


def test_status_marker_cannot_be_deleted(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    doc = root / "infrastructure/README.md"
    doc.write_text(doc.read_text().replace(START, "").replace(END, ""))
    with pytest.raises(ControlError, match="expected one documentation status block"):
        synchronize(root)


def test_manual_falsification_fails_closed(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    path = root / "README.md"
    path.write_text(path.read_text().replace("`PLANNED / PENDING`", "`READY / GO`", 1))
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
