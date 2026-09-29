"""Admission, lifecycle, resource inventory and source-only action authority."""

import ast
import json
from pathlib import Path

from audit_lifecycle import assert_audit_lifecycle_consistent

from nexus_ai.compliance.contracts import INCOMPLETE_RESOURCES, resource_inventory


def test_compliance_admission_and_stage_a_state():
    manifest = json.loads(Path(".nxs/phases/NXS-P21.json").read_text())
    state = json.loads(Path(".nxs/project-state.json").read_text())
    requirements = json.loads(Path(".nxs/requirements.json").read_text())["requirements"]
    requirement = next(row for row in requirements if row["id"] == "NXS-COMP-001")
    assert manifest["status"] in {"BUILDING", "VALIDATING", "READY"}
    closed = manifest["status"] == "READY"
    assert manifest["decision"] == ("GO" if closed else "PENDING")
    assert requirement["status"] == ("VALIDATED" if closed else "IN_PROGRESS")
    if closed:
        assert state["active_phase"] in {None, "NXS-P22"}
    else:
        assert state["active_phase"] == "NXS-P21"
    if not closed:
        assert manifest["implementation_commit"] is None
        assert manifest["closure_commit"] is None
    registry = json.loads(Path(".nxs/phase-registry.json").read_text())["phases"]
    for phase in registry:
        if phase["id"] == "NXS-P22" and phase["status"] != "PLANNED":
            assert closed
            assert_audit_lifecycle_consistent()
        elif int(phase["id"].split("P")[-1]) >= 22:
            assert phase["status"] == "PLANNED" and phase["decision"] == "PENDING"
    if state["active_phase"] == "NXS-P22":
        assert_audit_lifecycle_consistent()


def test_resource_inventory_does_not_claim_universal_coverage():
    rows = resource_inventory()
    assert len(rows) <= 16
    supported = [row["resource_class"] for row in rows if row["adapter_status"] == "SUPPORTED"]
    assert supported == ["CUSTOMER_PROFILE"]
    credentials = next(row for row in rows if row["resource_class"] == "CREDENTIALS")
    assert credentials["classification"] == "CREDENTIAL"
    assert credentials["adapter_status"] == "NOT_APPLICABLE"
    assert "CREDENTIALS" not in INCOMPLETE_RESOURCES
    assert set(INCOMPLETE_RESOURCES) == {
        row["resource_class"] for row in rows if row["adapter_status"] == "DEFERRED"
    }
    assert len(INCOMPLETE_RESOURCES) == 9


def test_compliance_dependencies_bind_validated_domain_authorities():
    manifest = json.loads(Path(".nxs/phases/NXS-P21.json").read_text())
    registry = json.loads(Path(".nxs/phase-registry.json").read_text())["phases"]
    registered = next(row for row in registry if row["id"] == "NXS-P21")
    assert manifest["dependencies"] == registered["dependencies"] == ["NXS-P03", "NXS-P06"]
    requirements = {
        row["id"]: row
        for row in json.loads(Path(".nxs/requirements.json").read_text())["requirements"]
    }
    dependencies = requirements["NXS-COMP-001"]["dependencies"]
    assert dependencies == [
        "NXS-AUTH-006",
        "NXS-AUTH-007",
        "NXS-TENANT-003",
        "NXS-CUSTOMER-001",
        "NXS-EVENT-003",
    ]
    assert all(requirements[identity]["status"] == "VALIDATED" for identity in dependencies)


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
