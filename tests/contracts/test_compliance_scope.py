"""Admission, lifecycle, resource inventory and source-only action authority."""

import ast
import json
from pathlib import Path

from nexus_ai.compliance.contracts import resource_inventory


def test_compliance_admission_and_stage_a_state():
    manifest = json.loads(Path(".nxs/phases/NXS-P21.json").read_text())
    state = json.loads(Path(".nxs/project-state.json").read_text())
    requirements = json.loads(Path(".nxs/requirements.json").read_text())["requirements"]
    requirement = next(row for row in requirements if row["id"] == "NXS-COMP-001")
    assert manifest["status"] in {"BUILDING", "VALIDATING", "READY"}
    closed = manifest["status"] == "READY"
    assert manifest["decision"] == ("GO" if closed else "PENDING")
    assert requirement["status"] == ("VALIDATED" if closed else "IN_PROGRESS")
    assert state["active_phase"] == (None if closed else "NXS-P21")
    if not closed:
        assert manifest["implementation_commit"] is None
        assert manifest["closure_commit"] is None
    registry = json.loads(Path(".nxs/phase-registry.json").read_text())["phases"]
    for phase in registry:
        if int(phase["id"].split("P")[-1]) >= 22:
            assert phase["status"] == "PLANNED" and phase["decision"] == "PENDING"


def test_resource_inventory_does_not_claim_universal_coverage():
    rows = resource_inventory()
    assert len(rows) <= 16
    supported = [row["resource_class"] for row in rows if row["adapter_status"] == "SUPPORTED"]
    assert supported == ["CUSTOMER_PROFILE"]
    credentials = next(row for row in rows if row["resource_class"] == "CREDENTIALS")
    assert credentials["classification"] == "CREDENTIAL"
    assert credentials["adapter_status"] == "DEFERRED"


def test_no_external_or_model_execution_authority():
    forbidden = {"subprocess", "httpx", "requests", "paramiko", "importlib", "socket"}
    for path in Path("src/nexus_ai/compliance").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] not in forbidden for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert module.split(".")[0] not in forbidden
                assert not module.startswith(
                    ("nexus_ai.sentinel", "nexus_ai.agents", "nexus_ai.tools")
                )
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"eval", "exec", "__import__"}
