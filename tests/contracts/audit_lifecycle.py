"""Strict P22 successor lifecycle assertions shared by phase contracts."""

import json
import re
from pathlib import Path


def assert_audit_lifecycle_consistent():
    manifest_path = Path(".nxs/phases/NXS-P22.json")
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text())
    state = json.loads(Path(".nxs/project-state.json").read_text())
    registry = {
        row["id"]: row for row in json.loads(Path(".nxs/phase-registry.json").read_text())["phases"]
    }
    requirements = {
        row["id"]: row
        for row in json.loads(Path(".nxs/requirements.json").read_text())["requirements"]
    }
    requirement = requirements["NXS-AUDIT-002"]
    assert manifest["id"] == "NXS-P22"
    assert manifest["status"] in {"BUILDING", "VALIDATING", "READY"}
    closed = manifest["status"] == "READY"
    assert manifest["decision"] == ("GO" if closed else "PENDING")
    assert requirement["status"] == ("VALIDATED" if closed else "IN_PROGRESS")
    assert requirement["mandatory"] is True
    assert requirement["target_phase"] == "NXS-P22"
    assert manifest["requirements_implemented"] == ["NXS-AUDIT-002"]
    assert (
        manifest["dependencies"]
        == registry["NXS-P22"]["dependencies"]
        == ["NXS-P04", "NXS-P20", "NXS-P21"]
    )
    for dependency in ("NXS-P04", "NXS-P20", "NXS-P21"):
        assert registry[dependency]["status"] == "READY"
        assert registry[dependency]["decision"] == "GO"
        assert dependency in state["completed_phases"]
    assert manifest["branch"] == registry["NXS-P22"]["branch"] == "feat/nxs-p22-audit"
    for field in ("status", "decision"):
        assert registry["NXS-P22"][field] == manifest[field]
    for field in ("id", "status", "decision", "branch", "implementation_commit", "closure_commit"):
        assert state["current_phase"][field] == manifest[field]
    assert state["active_phase"] == (None if closed else "NXS-P22")
    assert ("NXS-P22" in state["completed_phases"]) is closed
    if closed:
        assert isinstance(manifest["implementation_commit"], str)
        assert re.fullmatch(r"[0-9a-f]{40}", manifest["implementation_commit"])
        if manifest["closure_commit"] is not None:
            assert isinstance(manifest["closure_commit"], str)
            assert re.fullmatch(r"[0-9a-f]{40}", manifest["closure_commit"])
    else:
        assert manifest["implementation_commit"] is None
        assert manifest["closure_commit"] is None
    for phase in registry.values():
        if int(phase["id"].split("P")[-1]) >= 23:
            assert phase["status"] == "PLANNED"
            assert phase["decision"] == "PENDING"
