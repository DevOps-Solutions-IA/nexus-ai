"""P20-B is platform persistence, not an executor or lifecycle certification."""

import ast
import json
from pathlib import Path


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
        "nexus_ai.tenancy",
        "nexus_ai.telephony",
        "nexus_ai.integrations.credentials",
        "subprocess",
        "httpx",
        "socket",
        "paramiko",
    ):
        assert not any(item.startswith(prefix) for item in imports), prefix
    assert {item for item in imports if item.startswith("nexus_ai.agents")} <= {
        "nexus_ai.agents.models.base"
    }


def test_foundation_not_phase_certification():
    manifest = json.loads(Path(".nxs/phases/NXS-P20.json").read_text())
    state = json.loads(Path(".nxs/project-state.json").read_text())
    requirements = json.loads(Path(".nxs/requirements.json").read_text())["requirements"]
    assert manifest["status"] == "BUILDING" and manifest["decision"] == "PENDING"
    assert manifest["implementation_commit"] is None and manifest["closure_commit"] is None
    assert state["active_phase"] == "NXS-P20"
    assert (
        next(item for item in requirements if item["id"] == "NXS-SRE-001")["status"]
        == "IN_PROGRESS"
    )
    registry = json.loads(Path(".nxs/phase-registry.json").read_text())["phases"]
    for phase in registry:
        if int(phase["id"].split("P")[-1]) >= 21:
            assert phase["status"] == "PLANNED" and phase["decision"] == "PENDING"
