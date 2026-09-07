"""A fresh agent must reconstruct phase status from the repository alone (sections 72, 85)."""

from __future__ import annotations

from pathlib import Path

from scripts.nxs_control.core import (
    load_json,
    next_allowed_execution,
    next_eligible_phase,
    registry_phases,
)

ROOT = Path(__file__).parents[2]


def test_agent_reconstructs_status_from_repository_only() -> None:
    state = load_json(ROOT / ".nxs/project-state.json")
    registry = registry_phases(ROOT)
    requirements = load_json(ROOT / ".nxs/requirements.json")

    assert state["product"]["name"] == "Nexus AI"

    # Completed phases are exactly the READY/GO phases in the registry.
    completed = set(state["completed_phases"])
    assert completed == {pid for pid, p in registry.items() if p["status"] == "READY"}
    for phase_id in completed:
        assert registry[phase_id]["decision"] == "GO"

    # next_allowed_execution is reproducible purely from the registry DAG and active phase.
    assert state["next_allowed_execution"] == next_allowed_execution(ROOT)

    active = state["active_phase"]
    if active is not None:
        assert registry[active]["status"] in {"BUILDING", "VALIDATING"}
        assert state["next_allowed_execution"] == {
            "phase": active,
            "condition": "ACTIVE_PHASE_ONLY",
        }
    else:
        candidate = next_eligible_phase(ROOT)
        if candidate is not None:
            phase = registry[candidate]
            assert phase["branch"]
            for dependency in phase["dependencies"]:
                assert registry[dependency]["status"] == "READY"
                assert registry[dependency]["decision"] == "GO"

    # Future capabilities must never be dropped from the ledger.
    ledger_ids = {r["id"] for r in requirements["requirements"]}
    for preserved in ("NXS-CAP-001", "NXS-ORG-001", "NXS-VOICE-001", "NXS-DR-001", "NXS-SRE-001"):
        assert preserved in ledger_ids

    # Once P01 is READY, the next phase is deterministically P02.
    if registry["NXS-P01"]["status"] == "READY" and active is None:
        assert next_eligible_phase(ROOT) == "NXS-P02"
        assert registry["NXS-P02"]["branch"] == "feat/nxs-p02-tenancy"
