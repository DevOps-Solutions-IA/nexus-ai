"""P22 direct platform authority dependencies cannot remain implicit."""

import ast
import json
from pathlib import Path

from scripts.nxs_control.core import dependencies_ready


def test_p22_declares_sentinel_phase_and_validated_requirement_authority():
    registry = {
        item["id"]: item
        for item in json.loads(Path(".nxs/phase-registry.json").read_text())["phases"]
    }
    requirements = {
        item["id"]: item
        for item in json.loads(Path(".nxs/requirements.json").read_text())["requirements"]
    }
    manifest = json.loads(Path(".nxs/phases/NXS-P22.json").read_text())
    expected = ["NXS-P04", "NXS-P20", "NXS-P21"]
    assert manifest["dependencies"] == registry["NXS-P22"]["dependencies"] == expected
    for dependency in expected:
        assert registry[dependency]["status"] == "READY"
        assert registry[dependency]["decision"] == "GO"
    assert "NXS-SRE-001" in requirements["NXS-AUDIT-002"]["dependencies"]
    assert requirements["NXS-SRE-001"]["target_phase"] == "NXS-P20"
    for dependency in requirements["NXS-AUDIT-002"]["dependencies"]:
        assert requirements[dependency]["status"] == "VALIDATED"
    assert dependencies_ready(registry, registry["NXS-P22"])
    registry["NXS-P20"]["decision"] = "PENDING"
    assert not dependencies_ready(registry, registry["NXS-P22"])


def test_platform_grant_seam_has_no_certified_production_caller():
    constructions = []
    for root in (Path("src"), Path("scripts")):
        for path in root.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Attribute):
                    assert node.func.attr != "grant_create_capability", (
                        "Production grants require authenticated authority and durable audit",
                        str(path),
                    )
                if isinstance(node.func, ast.Name) and node.func.id == "PlatformGrantRecord":
                    constructions.append(str(path))
    assert constructions == ["src/nexus_ai/domain/provisioning/repository.py"]


def test_platform_grant_attribution_is_not_promoted_to_audit_actor():
    for path in Path("src/nexus_ai/audit").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr != "granted_by_user_id", str(path)
    design = Path("docs/engineering/nxs-p22-audit-design.md").read_text()
    assert "no certified authenticated grant/revoke administration surface" in design
    assert "granted_by_user_id is nullable caller-supplied FK metadata" in design
