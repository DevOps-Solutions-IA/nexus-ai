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
    ledger_by_id = {r["id"]: r for r in requirements["requirements"]}
    for preserved in ("NXS-CAP-001", "NXS-ORG-001", "NXS-VOICE-001", "NXS-DR-001", "NXS-SRE-001"):
        assert preserved in ledger_by_id
    # Organization provisioning belongs to P05 — no earlier phase claims it. While P05
    # is active or closed the requirement follows its lifecycle; otherwise PLANNED.
    assert ledger_by_id["NXS-ORG-001"]["target_phase"] == "NXS-P05"
    if registry["NXS-P05"]["status"] == "PLANNED":
        assert ledger_by_id["NXS-ORG-001"]["status"] == "PLANNED"
    else:
        assert ledger_by_id["NXS-ORG-001"]["status"] in {"IN_PROGRESS", "IMPLEMENTED", "VALIDATED"}

    # The next eligible phase is generic: the first non-READY, non-blocked phase in
    # registry order whose dependencies are all READY/GO. Asserted generically so
    # closing ANY phase (P02 → P03 → P04 → ...) keeps this contract true without
    # per-phase edits.
    if active is None and candidate is not None:
        blocked = set(state.get("blocked_phases", []))
        phases_in_order = load_json(ROOT / ".nxs/phase-registry.json")["phases"]
        expected: str | None = None
        for entry in phases_in_order:
            phase_id = entry["id"]
            if registry[phase_id]["status"] == "READY" or phase_id in blocked:
                continue
            if all(
                registry[dependency]["status"] == "READY"
                and registry[dependency]["decision"] == "GO"
                for dependency in entry["dependencies"]
            ):
                expected = phase_id
                break
        assert candidate == expected, (
            "the first eligible phase must match the generic DAG computation"
        )

    # Phase-specific expectations, each guarded by the relevant closed predecessor.
    if registry["NXS-P01"]["status"] == "READY" and registry["NXS-P02"]["status"] != "READY":
        if active is None:
            assert next_eligible_phase(ROOT) == "NXS-P02"
        assert registry["NXS-P02"]["branch"] == "feat/nxs-p02-tenancy"

    if registry["NXS-P02"]["status"] == "READY" and registry["NXS-P03"]["status"] != "READY":
        if active is None:
            assert next_eligible_phase(ROOT) == "NXS-P03"
        assert registry["NXS-P03"]["branch"] == "feat/nxs-p03-security-auth"

    if registry["NXS-P03"]["status"] == "READY" and registry["NXS-P04"]["status"] != "READY":
        if active is None:
            assert next_eligible_phase(ROOT) == "NXS-P04"
        assert registry["NXS-P04"]["branch"] == "feat/nxs-p04-data-events"

    # Once P04 is READY — and P05 has not closed yet — the next phase is P05.
    if (
        registry["NXS-P04"]["status"] == "READY"
        and registry["NXS-P05"]["status"] != "READY"
        and active is None
    ):
        assert next_eligible_phase(ROOT) == "NXS-P05"
        assert registry["NXS-P05"]["branch"] == "feat/nxs-p05-provisioner-dashboard"

    # Once P05 is READY — and P06 has not closed yet — the next phase is P06.
    if (
        registry["NXS-P05"]["status"] == "READY"
        and registry["NXS-P06"]["status"] != "READY"
        and active is None
    ):
        assert next_eligible_phase(ROOT) == "NXS-P06"
        assert registry["NXS-P06"]["branch"] == "feat/nxs-p06-customer-conversations"

    # Once P06 is READY — and P07 has not closed yet — the next phase is P07.
    if (
        registry["NXS-P06"]["status"] == "READY"
        and registry["NXS-P07"]["status"] != "READY"
        and active is None
    ):
        assert next_eligible_phase(ROOT) == "NXS-P07"
        assert registry["NXS-P07"]["branch"] == "feat/nxs-p07-integration-hub"

    # Once P07 is READY — and P08 has not closed yet — the next phase is P08.
    if (
        registry["NXS-P07"]["status"] == "READY"
        and registry["NXS-P08"]["status"] != "READY"
        and active is None
    ):
        assert next_eligible_phase(ROOT) == "NXS-P08"
        assert registry["NXS-P08"]["branch"] == "feat/nxs-p08-tool-engine"

    # Once P08 is READY — and P09 has not closed yet — the next phase is P09.
    if (
        registry["NXS-P08"]["status"] == "READY"
        and registry["NXS-P09"]["status"] != "READY"
        and active is None
    ):
        assert next_eligible_phase(ROOT) == "NXS-P09"
        assert registry["NXS-P09"]["branch"] == "feat/nxs-p09-messaging"

    # Once P09 is READY — and P10 has not closed yet — the next phase is P10.
    if (
        registry["NXS-P09"]["status"] == "READY"
        and registry["NXS-P10"]["status"] != "READY"
        and active is None
    ):
        assert next_eligible_phase(ROOT) == "NXS-P10"
        assert registry["NXS-P10"]["branch"] == "feat/nxs-p10-otp"

    # Once P10 is READY — and P11 has not closed yet — the next phase is P11.
    if (
        registry["NXS-P10"]["status"] == "READY"
        and registry["NXS-P11"]["status"] != "READY"
        and active is None
    ):
        assert next_eligible_phase(ROOT) == "NXS-P11"
        assert registry["NXS-P11"]["branch"] == "feat/nxs-p11-telephony"

    # Once P11 is READY — and P12 has not closed yet — the next phase is P12.
    if (
        registry["NXS-P11"]["status"] == "READY"
        and registry["NXS-P12"]["status"] != "READY"
        and active is None
    ):
        assert next_eligible_phase(ROOT) == "NXS-P12"
        assert registry["NXS-P12"]["branch"] == "feat/nxs-p12-elevenlabs"

    # Once P12 is READY — and P13 has not closed yet — the next phase is P13.
    if (
        registry["NXS-P12"]["status"] == "READY"
        and registry["NXS-P13"]["status"] != "READY"
        and active is None
    ):
        assert next_eligible_phase(ROOT) == "NXS-P13"
        assert registry["NXS-P13"]["branch"] == "feat/nxs-p13-agent-runtime"

    # Once P13 is READY, the next phase is deterministically P14 (the first
    # non-READY phase in registry order whose dependencies are all READY).
    if registry["NXS-P13"]["status"] == "READY" and active is None:
        assert next_eligible_phase(ROOT) == "NXS-P14"
        assert registry["NXS-P14"]["branch"] == "feat/nxs-p14-workflows"
