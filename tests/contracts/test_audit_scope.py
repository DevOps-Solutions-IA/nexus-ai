"""P22 admission, canonical lifecycle and audit authority invariants."""

import ast
import json
import re
from pathlib import Path

import pytest
from audit_lifecycle import assert_audit_lifecycle_consistent
from test_compliance_scope import (
    test_compliance_admission_and_stage_a_state as assert_compliance_lifecycle_consistent,
)
from test_sentinel_scope import (
    test_p20_lifecycle_state_is_consistent as assert_sentinel_lifecycle_consistent,
)


def test_audit_admission_and_lifecycle_are_consistent():
    assert_audit_lifecycle_consistent()


@pytest.mark.parametrize(
    ("status", "document", "field", "value", "valid"),
    [
        ("BUILDING", None, None, None, True),
        ("VALIDATING", None, None, None, True),
        ("READY", None, None, None, True),
        ("READY", "manifest", "closure_commit", "b" * 40, True),
        ("BUILDING", "manifest", "decision", "GO", False),
        ("VALIDATING", "manifest", "decision", "GO", False),
        ("READY", "manifest", "decision", "PENDING", False),
        ("BUILDING", "manifest", "implementation_commit", "a" * 40, False),
        ("VALIDATING", "manifest", "implementation_commit", "a" * 40, False),
        ("BUILDING", "manifest", "closure_commit", "b" * 40, False),
        ("VALIDATING", "manifest", "closure_commit", "b" * 40, False),
        ("READY", "manifest", "implementation_commit", None, False),
        ("READY", "manifest", "implementation_commit", "A" * 40, False),
        ("READY", "manifest", "implementation_commit", "a" * 39, False),
        ("READY", "manifest", "closure_commit", "short", False),
        ("BUILDING", "requirement", "status", "VALIDATED", False),
        ("VALIDATING", "requirement", "status", "VALIDATED", False),
        ("READY", "requirement", "status", "IN_PROGRESS", False),
        ("BUILDING", "requirement", "mandatory", False, False),
        ("BUILDING", "requirement", "target_phase", "NXS-P23", False),
        ("BUILDING", "manifest", "requirements_implemented", [], False),
        ("BUILDING", "manifest", "dependencies", ["NXS-P04"], False),
        ("BUILDING", "manifest", "branch", "main", False),
        ("BUILDING", "registered", "status", "PLANNED", False),
        ("VALIDATING", "registered", "status", "BUILDING", False),
        ("READY", "registered", "decision", "PENDING", False),
        ("BUILDING", "registered", "dependencies", ["NXS-P21"], False),
        ("BUILDING", "registered", "branch", "main", False),
        ("BUILDING", "state", "active_phase", None, False),
        ("VALIDATING", "state", "active_phase", None, False),
        ("READY", "state", "active_phase", "NXS-P22", False),
        ("BUILDING", "state", "completed_phases", ["NXS-P04", "NXS-P21", "NXS-P22"], False),
        ("READY", "state", "completed_phases", ["NXS-P04", "NXS-P21"], False),
        ("BUILDING", "current", "id", "NXS-P21", False),
        ("BUILDING", "current", "status", "PLANNED", False),
        ("VALIDATING", "current", "decision", "GO", False),
        ("READY", "current", "implementation_commit", None, False),
        ("READY", "current", "closure_commit", "c" * 40, False),
        ("BUILDING", "dependency", "status", "BUILDING", False),
        ("BUILDING", "dependency", "decision", "PENDING", False),
        ("BUILDING", "future", "status", "BUILDING", False),
        ("READY", "future", "status", "READY", False),
        ("READY", "future", "decision", "GO", False),
        ("PLANNED", None, None, None, False),
        ("FAILED", None, None, None, False),
        ("BLOCKED", None, None, None, False),
    ],
)
def test_audit_lifecycle_contract_rejects_inconsistent_combinations(
    tmp_path, monkeypatch, status, document, field, value, valid
):
    closed = status == "READY"
    manifest = {
        "id": "NXS-P22",
        "status": status,
        "decision": "GO" if closed else "PENDING",
        "branch": "feat/nxs-p22-audit",
        "implementation_commit": "a" * 40 if closed else None,
        "closure_commit": None,
        "requirements_implemented": ["NXS-AUDIT-002"],
        "dependencies": ["NXS-P04", "NXS-P21"],
    }
    registered = {key: manifest[key] for key in ("id", "status", "decision", "branch")}
    registered["dependencies"] = ["NXS-P04", "NXS-P21"]
    current = {
        key: manifest[key]
        for key in ("id", "status", "decision", "branch", "implementation_commit", "closure_commit")
    }
    state = {
        "active_phase": None if closed else "NXS-P22",
        "current_phase": current,
        "completed_phases": ["NXS-P04", "NXS-P21"] + (["NXS-P22"] if closed else []),
    }
    requirement = {
        "id": "NXS-AUDIT-002",
        "status": "VALIDATED" if closed else "IN_PROGRESS",
        "mandatory": True,
        "target_phase": "NXS-P22",
    }
    dependency = {"id": "NXS-P21", "status": "READY", "decision": "GO"}
    future = {"id": "NXS-P23", "status": "PLANNED", "decision": "PENDING"}
    if document is not None:
        documents = {
            "manifest": manifest,
            "registered": registered,
            "state": state,
            "current": current,
            "requirement": requirement,
            "dependency": dependency,
            "future": future,
        }
        documents[document][field] = value
        if document == "manifest" and field == "closure_commit" and valid:
            current["closure_commit"] = value
    (tmp_path / ".nxs/phases").mkdir(parents=True)
    for name, payload in {
        "phases/NXS-P22": manifest,
        "project-state": state,
        "phases/NXS-P20": {
            "status": "READY",
            "decision": "GO",
            "implementation_commit": "d" * 40,
            "closure_commit": None,
        },
        "phases/NXS-P21": {
            "status": "READY",
            "decision": "GO",
            "implementation_commit": "e" * 40,
            "closure_commit": None,
        },
        "requirements": {
            "requirements": [
                requirement,
                {"id": "NXS-SRE-001", "status": "VALIDATED"},
                {"id": "NXS-COMP-001", "status": "VALIDATED"},
            ]
        },
        "phase-registry": {
            "phases": [
                {"id": "NXS-P04", "status": "READY", "decision": "GO"},
                dependency,
                registered,
                future,
            ]
        },
    }.items():
        (tmp_path / f".nxs/{name}.json").write_text(json.dumps(payload))
    monkeypatch.chdir(tmp_path)
    for contract in (
        assert_audit_lifecycle_consistent,
        assert_sentinel_lifecycle_consistent,
        assert_compliance_lifecycle_consistent,
    ):
        if valid:
            contract()
        else:
            with pytest.raises(AssertionError):
                contract()


@pytest.mark.parametrize(
    "path",
    ["docs/adr/0103-audit-platform-authority.md", "docs/engineering/nxs-p22-audit-design.md"],
)
def test_audit_docs_preserve_no_fictitious_tenant_or_global_transactional_claim(path):
    content = " ".join(Path(path).read_text().split())
    assert re.search(r"no (?:unproven )?universal", content, re.IGNORECASE)
    assert "login Organization" in content
    assert re.search(r"(?:inventing an Organization|fake system Organization)", content)
    assert "publish_global is direct confirmed publication, not" in content
    assert "transactional" in content


def test_audit_durable_capture_does_not_use_direct_global_publication():
    sources = list(Path("src/nexus_ai/audit").glob("*.py"))
    assert sources
    for source in sources:
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr != "publish_global", source
