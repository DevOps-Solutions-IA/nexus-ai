"""SIP placement consumes existing authorities without expanding phase ownership."""

import json
from pathlib import Path

from nexus_ai.cells.contracts import PlacementOperation
from nexus_ai.sip_edge.api import EgressEnvelope, InboundEnvelope
from nexus_ai.sip_edge.contracts import InboundRequest, PermitState, RouteState, TargetState
from nexus_ai.telephony.entities import CreateCallRequest


def test_sip_edge_scope_and_single_requirement_mapping() -> None:
    manifest = json.loads(Path(".nxs/phases/NXS-P19.json").read_text())
    assert manifest["requirements_implemented"] == ["NXS-SCALE-002"]
    assert manifest["dependencies"] == ["NXS-P11", "NXS-P18"]
    exclusions = " ".join(manifest["non_scope"])
    for boundary in (
        "P20",
        "P25",
        "P28",
        "P29",
        "P32",
        "relocation",
        "media migration",
        "capacity",
    ):
        assert boundary in exclusions
    requirements = json.loads(Path(".nxs/requirements.json").read_text())["requirements"]
    requirement = next(item for item in requirements if item["id"] == "NXS-SCALE-002")
    assert requirement["target_phase"] == "NXS-P19"
    assert set(requirement["dependencies"]) == {"NXS-TEL-001", "NXS-SCALE-001"}
    assert set(PlacementOperation) == {"ASSIGN", "SUSPEND", "RESUME"}


def test_no_tenant_route_selection_or_public_permit_injection() -> None:
    forbidden = {"organization_id", "cell_id", "target_host", "upstream_id", "placement_generation"}
    for model in (InboundEnvelope, InboundRequest, EgressEnvelope):
        assert model.model_config["extra"] == "forbid"
        assert not forbidden.intersection(model.model_fields)
    assert "sip_egress_permit" not in CreateCallRequest.model_fields
    assert "NXS_SIP_EGRESS_PERMIT" not in CreateCallRequest.model_fields
    assert set(TargetState) == {"REGISTERED", "ACTIVE", "DRAINING", "RETIRED"}
    assert "RELOCATED" not in RouteState
    assert "FAILOVER" not in PermitState
