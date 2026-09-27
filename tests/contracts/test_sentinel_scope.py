"""Sentinel authority boundaries and canonical P20 lifecycle consistency."""

import ast
import json
import re
from pathlib import Path

import pytest


def test_foundation_does_not_import_tenant_execution_or_network_dispatch():
    imports = set()
    for source in Path("src/nexus_ai/sentinel").glob("*.py"):
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
            elif isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
    for prefix in (
        "nexus_ai.tools",
        "nexus_ai.agents.runtime",
        "nexus_ai.agents.service",
        "nexus_ai.agents.toolbridge",
        "nexus_ai.domain.agents",
        "nexus_ai.events",
        "nexus_ai.tenancy",
        "nexus_ai.telephony",
        "subprocess",
        "httpx",
        "socket",
        "paramiko",
    ):
        assert not any(item.startswith(prefix) for item in imports), prefix
    assert {item for item in imports if item.startswith("nexus_ai.agents")} <= {
        "nexus_ai.agents.models.base",
        "nexus_ai.agents.models.openai_compatible",
    }
    for source in Path("src/nexus_ai/sentinel").glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "nexus_ai.integrations.credentials"
            ):
                assert {alias.name for alias in node.names} <= {"SecretMaterial", "CredentialType"}


def test_p20_lifecycle_state_is_consistent():
    manifest = json.loads(Path(".nxs/phases/NXS-P20.json").read_text())
    state = json.loads(Path(".nxs/project-state.json").read_text())
    requirements = json.loads(Path(".nxs/requirements.json").read_text())["requirements"]
    assert manifest["status"] in {"BUILDING", "VALIDATING", "READY"}
    closed = manifest["status"] == "READY"
    assert manifest["decision"] == ("GO" if closed else "PENDING")
    if closed:
        assert isinstance(manifest["implementation_commit"], str)
        assert re.fullmatch(r"[0-9a-f]{40}", manifest["implementation_commit"])
        if manifest["closure_commit"] is not None:
            assert isinstance(manifest["closure_commit"], str)
            assert re.fullmatch(r"[0-9a-f]{40}", manifest["closure_commit"])
    else:
        assert manifest["implementation_commit"] is None
        assert manifest["closure_commit"] is None
    assert state["active_phase"] == (None if closed else "NXS-P20")
    requirement = next(item for item in requirements if item["id"] == "NXS-SRE-001")
    assert requirement["status"] == ("VALIDATED" if closed else "IN_PROGRESS")
    registry = json.loads(Path(".nxs/phase-registry.json").read_text())["phases"]
    for phase in registry:
        if int(phase["id"].split("P")[-1]) >= 21:
            assert phase["status"] == "PLANNED" and phase["decision"] == "PENDING"


@pytest.mark.parametrize(
    ("status", "field", "value", "valid"),
    [
        ("BUILDING", None, None, True),
        ("VALIDATING", None, None, True),
        ("READY", None, None, True),
        ("READY", "closure_commit", "b" * 40, True),
        ("BUILDING", "decision", "GO", False),
        ("VALIDATING", "decision", "GO", False),
        ("READY", "decision", "PENDING", False),
        ("READY", "requirement", "IN_PROGRESS", False),
        ("BUILDING", "requirement", "VALIDATED", False),
        ("VALIDATING", "requirement", "VALIDATED", False),
        ("VALIDATING", "implementation_commit", "a" * 40, False),
        ("BUILDING", "closure_commit", "b" * 40, False),
        ("READY", "implementation_commit", None, False),
        ("READY", "implementation_commit", "A" * 40, False),
        ("READY", "implementation_commit", "a" * 39, False),
        ("READY", "closure_commit", "short", False),
        ("READY", "active_phase", "NXS-P20", False),
        ("VALIDATING", "active_phase", None, False),
        ("VALIDATING", "future_status", "BUILDING", False),
        ("READY", "future_status", "READY", False),
        ("READY", "future_decision", "GO", False),
        ("FAILED", None, None, False),
        ("BLOCKED", None, None, False),
    ],
)
def test_p20_lifecycle_contract_accepts_only_consistent_states(
    tmp_path, monkeypatch, status, field, value, valid
):
    closed = status == "READY"
    manifest = {
        "status": status,
        "decision": "GO" if closed else "PENDING",
        "implementation_commit": "a" * 40 if closed else None,
        "closure_commit": None,
    }
    state = {"active_phase": None if closed else "NXS-P20"}
    requirement = {"id": "NXS-SRE-001", "status": "VALIDATED" if closed else "IN_PROGRESS"}
    future = {"id": "NXS-P21", "status": "PLANNED", "decision": "PENDING"}
    if field in manifest:
        manifest[field] = value
    elif field == "requirement":
        requirement["status"] = value
    elif field == "active_phase":
        state["active_phase"] = value
    elif field == "future_status":
        future["status"] = value
    elif field == "future_decision":
        future["decision"] = value
    (tmp_path / ".nxs/phases").mkdir(parents=True)
    for name, document in {
        "phases/NXS-P20": manifest,
        "project-state": state,
        "requirements": {"requirements": [requirement]},
        "phase-registry": {"phases": [future]},
    }.items():
        (tmp_path / f".nxs/{name}.json").write_text(json.dumps(document))
    monkeypatch.chdir(tmp_path)
    if valid:
        test_p20_lifecycle_state_is_consistent()
    else:
        with pytest.raises(AssertionError):
            test_p20_lifecycle_state_is_consistent()
