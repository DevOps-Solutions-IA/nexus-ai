from pathlib import Path

from scripts.nxs_control.core import load_json


def test_agent_b_reconstructs_status_from_repository_only() -> None:
    root = Path(__file__).parents[2]
    state = load_json(root / ".nxs/project-state.json")
    registry = load_json(root / ".nxs/phase-registry.json")
    requirements = load_json(root / ".nxs/requirements.json")
    assert state["product"]["name"] == "Nexus AI"
    assert state["active_phase"] == "NXS-P00"
    assert state["next_allowed_execution"]["phase"] == "NXS-P00"
    assert any(phase["id"] == "NXS-P01" for phase in registry["phases"])
    assert any(requirement["id"] == "NXS-CAP-001" for requirement in requirements["requirements"])
