"""Execute P18 criteria and record pytest outcomes using the established evidence runner."""

from __future__ import annotations

import fnmatch
import hashlib
import json
from pathlib import Path
from typing import Any

from scripts.nxs_control.core import git, iso_now
from scripts.nxs_p13_evidence import run

ROOT = Path(__file__).resolve().parents[1]
RESULTS_PATH = ROOT / ".nxs/runtime/p18-test-results.json"
EVIDENCE = ROOT / ".nxs/evidence/NXS-P18"
RESULTS: dict[str, str] = {}


def case(name: str) -> str:
    return f"*::{name}*"


CONCURRENCY = {
    "C01": [case("test_independent_first_assignments_have_one_winner")],
    "C02": [case("test_concurrent_equivalent_request_has_one_receipt")],
    "C03": [case("test_transition_key_conflicts_on_target_or_generation_change")],
    "C04": [case("test_concurrent_state_writers_change_generation_once")],
    "C05": [case("test_resolution_races_suspension_but_snapshot_never_authorizes")],
    "C06": [case("test_suspend_resume_never_revives_old_generation")],
    "C07": [
        case("test_cross_cell_update_rejected_by_database"),
        case("test_suspended_placement_cannot_be_relocated"),
    ],
    "C08": [case("test_retirement_versus_assignment")],
    "C09": [case("test_outbox_failure_rolls_back_placement_and_receipt")],
    "C10": [case("test_committed_response_loss_reconstructs_original_receipt")],
    "C11": [case("test_event_replay_and_forged_projection_never_grant_authority")],
    "C12": [
        case("test_restart_reconstructs_from_postgres_without_cache_or_bus"),
        case("test_bus_outage_does_not_change_placement_authority"),
        case("test_disconnected_database_never_uses_last_route"),
    ],
    "C13": [
        case("test_p13_model_permit_holds_placement_lock_in_same_transaction"),
        case("test_p09_authorized_first_completes_after_suspension"),
        case("test_p09_placement_denial_has_zero_provider_calls"),
    ],
    "C14": [
        case("test_worker_death_after_queued_authority_never_mints_new_identity"),
        case("test_workflow_result_after_suspend_does_not_unlock_new_steps"),
    ],
    "C15": [
        case("test_p09_placement_denial_has_zero_provider_calls"),
        case("test_cell_control_requires_platform_grant_and_ignores_forged_headers"),
    ],
    "C16": [
        case("test_p13_model_permit_holds_placement_lock_in_same_transaction"),
        case("test_p17_ai_return_cannot_grant_ownership_after_placement_suspends"),
        case("test_p17_send_cannot_authorize_from_suspended_cell"),
    ],
    "C17": [
        case("test_workflow_route_retry_keeps_exact_run_and_step_identity"),
        case("test_scheduler_route_retry_preserves_occurrence_and_p14_key"),
        case("test_campaign_stale_route_cannot_authorize_or_duplicate_send"),
        case("test_placed_campaign_scheduled_bridge_replays_exact_release"),
    ],
    "C18": [
        "tests/unit/test_cell_contracts.py::*",
        case("test_catalog_administration_is_bounded_and_authorized"),
    ],
}

ACCEPTANCE = {
    "AC01": [case("test_initial_assignment_replay_and_fingerprint_conflict")],
    "AC02": [case("test_direct_duplicate_placement_rejected_by_postgres")],
    "AC03": CONCURRENCY["C12"],
    "AC04": [
        case("test_runtime_rls_cannot_be_disabled_and_foreign_updates_change_zero_rows"),
        case("test_cross_tenant_sql_and_snapshot_fence"),
    ],
    "AC05": [case("test_composite_fk_and_distinct_uniqueness_contracts")],
    "AC06": CONCURRENCY["C01"],
    "AC07": CONCURRENCY["C04"],
    "AC08": CONCURRENCY["C06"],
    "AC09": CONCURRENCY["C02"] + CONCURRENCY["C03"] + CONCURRENCY["C10"],
    "AC10": [
        case("test_unknown_or_retired_worker_never_grants_admission"),
        case("test_retirement_is_absorbing_and_replay_has_one_history"),
    ],
    "AC11": [
        case("test_p09_placement_denial_has_zero_provider_calls"),
        case("test_rollout_is_explicit_and_legacy_worker_cannot_serve_placed_org"),
    ],
    "AC12": CONCURRENCY["C09"],
    "AC13": CONCURRENCY["C11"] + CONCURRENCY["C12"],
    "AC14": [
        case("test_p13_placement_preserves_session_and_turn_replay"),
        case("test_p13_model_permit_holds_placement_lock_in_same_transaction"),
        "tests/integration/test_agent_service.py::*",
    ],
    "AC15": [
        "tests/integration/test_human_operations_service.py::*",
        case("test_p17_claim_fenced_without_changing_ownership_or_capacity"),
        case("test_p17_ai_return_cannot_grant_ownership_after_placement_suspends"),
        case("test_p17_send_cannot_authorize_from_suspended_cell"),
    ],
    "AC16": ["tests/integration/test_cell_messaging.py::*"],
    "AC17": CONCURRENCY["C17"]
    + [
        "tests/integration/test_scheduler_service.py::*",
        "tests/integration/test_campaign_scheduled_release_bridge.py::*",
    ],
    "AC18": [
        "tests/security/test_tool_security.py::*",
        "tests/security/test_integration_security.py::*",
    ],
    "AC19": [pattern for patterns in CONCURRENCY.values() for pattern in patterns],
    "AC20": CONCURRENCY["C15"]
    + [
        case("test_organization_owner_is_not_platform_control"),
        case("test_runtime_without_control_cannot_mutate_catalog_or_placement"),
        case("test_composite_fk_and_distinct_uniqueness_contracts"),
    ],
    "AC21": [
        pattern for name, patterns in CONCURRENCY.items() if name >= "C09" for pattern in patterns
    ],
    "AC22": ["tests/integration/test_cell_migration.py::*"],
    "AC23": ["tests/contracts/test_cell_scope.py::*"],
}


def pytest_runtest_logreport(report: Any) -> None:
    if report.failed:
        RESULTS[report.nodeid] = "FAIL"
    elif report.skipped:
        RESULTS[report.nodeid] = "SKIP"
    elif report.when == "call" and report.nodeid not in RESULTS:
        RESULTS[report.nodeid] = "PASS"


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(
        json.dumps({"exit_code": int(exitstatus), "tests": RESULTS}, sort_keys=True) + "\n"
    )


def evaluate(mapping: dict[str, list[str]], results: dict[str, str]) -> dict[str, Any]:
    evaluated: dict[str, Any] = {}
    for identifier, patterns in mapping.items():
        matched = {
            node: status
            for node, status in results.items()
            if any(fnmatch.fnmatchcase(node, pattern) for pattern in patterns)
        }
        missing = [
            pattern
            for pattern in patterns
            if not any(fnmatch.fnmatchcase(node, pattern) for node in results)
        ]
        evaluated[identifier] = {
            "result": "PASS"
            if matched and not missing and set(matched.values()) == {"PASS"}
            else "FAIL",
            "tests": matched,
            "missing": missing,
        }
    return evaluated


def source_snapshot(root: Path, paths: list[str]) -> dict[str, str]:
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in sorted(paths)
        if (root / name).is_file() and not name.startswith((".nxs/evidence/", ".nxs/runtime/"))
    }


def main() -> int:
    RESULTS_PATH.unlink(missing_ok=True)
    tests = sorted(
        str(path.relative_to(ROOT)) for path in (ROOT / "tests/integration").glob("test_cell*.py")
    )
    tests += [
        "tests/unit/test_cell_contracts.py",
        "tests/contracts/test_cell_scope.py",
        "tests/security/test_tool_security.py",
        "tests/security/test_integration_security.py",
        "tests/integration/test_human_operations_service.py",
        "tests/integration/test_agent_service.py",
        "tests/integration/test_scheduler_service.py",
        "tests/integration/test_campaign_scheduled_release_bridge.py",
    ]
    arguments = ["pytest", "-q", "--no-cov", "-p", "scripts.nxs_p18_evidence", *tests]
    completed = run(*arguments)
    report = (
        json.loads(RESULTS_PATH.read_text())
        if RESULTS_PATH.exists()
        else {"tests": {}, "exit_code": 1}
    )
    concurrency = evaluate(CONCURRENCY, report["tests"])
    acceptance = evaluate(ACCEPTANCE, report["tests"])
    passed = (
        completed.returncode == 0
        and report["exit_code"] == 0
        and all(item["result"] == "PASS" for item in [*concurrency.values(), *acceptance.values()])
    )
    paths = git(ROOT, "ls-files", "--cached", "--others", "--exclude-standard").splitlines()
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0.0",
        "phase": "NXS-P18",
        "result": "PASS" if passed else "FAIL",
        "recorded_at": iso_now(),
        "base_commit": git(ROOT, "rev-parse", "HEAD"),
        "binding": "Uncommitted candidate content hashes; not an implementation commit or closure",
        "command": ["uv", "run", *arguments],
        "exit_code": completed.returncode,
        "output_tail": (completed.stdout + completed.stderr)[-2000:],
        "concurrency": concurrency,
        "acceptance": acceptance,
        "source_sha256": source_snapshot(ROOT, paths),
        "closure": "NOT_AUTHORIZED",
    }
    (EVIDENCE / "execution-criteria.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )
    print(f"NXS-P18 execution criteria: {payload['result']}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
