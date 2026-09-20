"""Execute the full suite and bind P19 obligations to actual pytest results."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from scripts.nxs_control.core import git, iso_now
from scripts.nxs_p13_evidence import run
from scripts.nxs_p18_evidence import evaluate, source_snapshot

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / ".nxs/evidence/NXS-P19"
RUNTIME = ROOT / ".nxs/runtime"
RESULTS: dict[str, str] = {}


def case(name: str) -> str:
    return f"*::{name}*"


WIRE = case("test_real_kamailio_invite_and_forged_route_zero_send")
EGRESS_WIRE = case("test_real_p11_ari_edge_permit_is_consumed_and_stripped")
STREAM_WIRE = case("test_real_stream_dialog_and_two_edge_authority")
LOCATOR = "tests/integration/test_sip_did_locator.py::*"
TARGETS = "tests/integration/test_sip_target_constraints.py::*"
PERMITS = case("test_egress_single_consumption_wrong_peer_destination_and_response_loss")
ROUTES = case("test_inbound_authority_issue_race_and_retirement_fence")
RACES = case("test_suspension_linearizes_with_new_route_admission")
UPSTREAMS = case("test_upstream_rotation_fences_old_permit_and_preserves_append_only_history")
AUTH = "tests/integration/test_sip_edge_authentication.py::*"
SCOPE = "tests/contracts/test_sip_edge_scope.py::*"

CONCURRENCY = {
    "C01": [case("test_concurrent_independent_edges_same_authoritative_tuple"), WIRE, STREAM_WIRE],
    "C02": [RACES],
    "C03": [case("test_reactivation_preserves_cell_and_advances_new_dialog_generation")],
    "C04": [WIRE, case("test_failures_create_zero_route_authority")],
    "C05": [case("test_failures_create_zero_route_authority")],
    "C06": [case("test_concurrent_target_activations_have_one_revision_winner")],
    "C07": [case("test_target_rotation_and_resolution_never_mix_tuple")],
    "C08": [WIRE, STREAM_WIRE],
    "C09": [EGRESS_WIRE, WIRE],
    "C10": [EGRESS_WIRE, UPSTREAMS],
    "C11": [case("test_failures_create_zero_route_authority"), WIRE],
    "C12": [WIRE],
    "C13": [WIRE, ROUTES],
    "C14": [WIRE, STREAM_WIRE],
    "C15": [WIRE, STREAM_WIRE],
    "C16": [WIRE, STREAM_WIRE],
    "C17": [WIRE],
    "C18": [LOCATOR, TARGETS, case("test_route_history_and_dialog_raw_sql_cannot_cross_tenant")],
    "C19": [WIRE, "tests/unit/test_sip_edge_contracts.py::*"],
    "C20": [WIRE, case("test_concurrent_independent_edges_same_authoritative_tuple")],
    "C21": [RACES, LOCATOR],
    "C22": [TARGETS],
    "C23": [
        case("test_route_reference_and_history_rollback_atomically"),
        WIRE,
        case("test_unissued_inbound_deadline_cannot_be_extended_or_relayed"),
    ],
    "C24": [ROUTES],
    "C25": [
        AUTH,
        ROUTES,
        case("test_route_history_and_dialog_raw_sql_cannot_cross_tenant"),
        case("test_cache_bus_absence_and_reordered_hints_cannot_grant_route_authority"),
    ],
    "C26": [case("test_shared_carrier_more_than_32_tenants")],
    "C27": [LOCATOR],
    "C28": [RACES, LOCATOR],
    "C29": [PERMITS, EGRESS_WIRE],
    "C30": [PERMITS, EGRESS_WIRE],
    "C31": [
        PERMITS,
        EGRESS_WIRE,
        UPSTREAMS,
        case("test_unconsumed_permit_expires_on_database_time_and_cannot_be_reissued"),
    ],
    "C32": [PERMITS, EGRESS_WIRE],
}

ACCEPTANCE = {
    "AC01": [SCOPE],
    "AC02": [LOCATOR],
    "AC03": [RACES, ROUTES],
    "AC04": CONCURRENCY["C02"] + CONCURRENCY["C03"],
    "AC05": [TARGETS, "tests/unit/test_sip_edge_contracts.py::*"],
    "AC06": CONCURRENCY["C06"] + [TARGETS],
    "AC07": CONCURRENCY["C07"] + CONCURRENCY["C22"],
    "AC08": ["tests/integration/test_sip_native_configuration.py::*", LOCATOR],
    "AC09": CONCURRENCY["C09"] + CONCURRENCY["C10"],
    "AC10": CONCURRENCY["C08"] + CONCURRENCY["C19"],
    "AC11": CONCURRENCY["C04"],
    "AC12": CONCURRENCY["C05"],
    "AC13": CONCURRENCY["C11"] + CONCURRENCY["C12"],
    "AC14": CONCURRENCY["C01"] + CONCURRENCY["C20"],
    "AC15": CONCURRENCY["C14"] + CONCURRENCY["C15"],
    "AC16": [
        "tests/integration/test_telephony*.py::*",
        EGRESS_WIRE,
        "tests/integration/test_sip_permit_issuance_failure.py::*",
    ],
    "AC17": ["tests/integration/test_voice*.py::*"],
    "AC18": [SCOPE, case("test_suspended_placement_cannot_be_relocated")],
    "AC19": CONCURRENCY["C23"] + [SCOPE],
    "AC20": [SCOPE],
    "AC21": ["tests/integration/test_sip_native_configuration.py::*"],
    "AC22": [case("test_native_pinned_configuration_on_both_architectures")],
    "AC23": [WIRE, EGRESS_WIRE, STREAM_WIRE],
    "AC24": [RACES, TARGETS, PERMITS],
    "AC25": CONCURRENCY["C16"] + CONCURRENCY["C09"] + [STREAM_WIRE],
    "AC26": ["tests/integration/test_agent_handoff.py::*"],
    "AC27": [SCOPE],
    "AC28": CONCURRENCY["C18"]
    + CONCURRENCY["C25"]
    + [case("test_explicit_backfill_is_bounded_idempotent_and_tenant_scoped")],
    "AC29": CONCURRENCY["C12"] + CONCURRENCY["C13"] + CONCURRENCY["C23"],
    "AC30": CONCURRENCY["C22"] + CONCURRENCY["C24"],
    "AC31": [LOCATOR],
    "AC32": CONCURRENCY["C28"],
    "AC33": CONCURRENCY["C27"],
    "AC34": CONCURRENCY["C26"],
    "AC35": [
        case("test_discovery_role_cannot_read_tenant_tables_or_list"),
        case("test_resolver_real_role_startup_health_and_shutdown"),
    ],
    "AC36": ["tests/integration/test_sip_native_configuration.py::*", LOCATOR],
    "AC37": CONCURRENCY["C26"],
    "AC38": [PERMITS, EGRESS_WIRE, SCOPE],
    "AC39": [case("test_actual_ari_variable_to_pjsip_header"), EGRESS_WIRE],
    "AC40": CONCURRENCY["C30"] + CONCURRENCY["C31"],
    "AC41": CONCURRENCY["C29"] + CONCURRENCY["C31"],
    "AC42": CONCURRENCY["C32"],
    "AC43": [UPSTREAMS, EGRESS_WIRE],
}


TRANSPORT = {
    "T01": [WIRE],
    "T02": ["*::test_real_stream_dialog_and_two_edge_authority[[]TCP-none-*]"],
    "T03": ["*::test_real_stream_dialog_and_two_edge_authority[[]TLS-none-*]"],
    "T04": [
        "*::test_real_stream_dialog_and_two_edge_authority[[]TLS-wrong*]",
        "*::test_real_stream_dialog_and_two_edge_authority[[]TLS-untrusted*]",
        "*::test_real_stream_dialog_and_two_edge_authority[[]TLS-missing*]",
        "*::test_real_stream_dialog_and_two_edge_authority[[]TLS-expired*]",
    ],
    "T05": ["*::test_real_stream_dialog_and_two_edge_authority[[]*downgrade*]"],
    "T06": [EGRESS_WIRE],
    "T07": ["tests/integration/test_sip_permit_issuance_failure.py::*"],
    "T08": ["tests/integration/test_sip_native_configuration.py::*"],
    "T09": [
        case("test_tls_target_pin_and_transport_are_immutable"),
        case("test_database_rejects_transport_identity_mismatch"),
        case("test_transport_requires_exact_tls_identity_only"),
        "tests/unit/test_sip_transport_uri.py::*",
        UPSTREAMS,
    ],
    "T10": ["*::test_real_stream_dialog_and_two_edge_authority[[]*cancel*]"],
}


def pytest_runtest_logreport(report: Any) -> None:
    if report.failed:
        RESULTS[report.nodeid] = "FAIL"
    elif report.skipped:
        RESULTS[report.nodeid] = "SKIP"
    elif report.when in {"call", "teardown"} and report.nodeid not in RESULTS:
        RESULTS[report.nodeid] = "PASS"


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    (RUNTIME / "p19-test-results.json").write_text(
        json.dumps({"exit_code": int(exitstatus), "tests": RESULTS}, sort_keys=True) + "\n"
    )


def main() -> int:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    report = RUNTIME / "p19-test-results.json"
    coverage_path = RUNTIME / "p19-coverage.json"
    paths = git(ROOT, "ls-files", "--cached", "--others", "--exclude-standard").splitlines()
    before = source_snapshot(ROOT, paths)
    command = [
        "pytest",
        "-p",
        "scripts.nxs_p19_evidence",
        "--cov-report=json:" + str(coverage_path),
        "--cov-report=term",
    ]
    report.unlink(missing_ok=True)
    started = time.monotonic()
    completed = run(*command)
    elapsed = time.monotonic() - started
    (RUNTIME / "p19-pytest-output.log").write_text(completed.stdout + completed.stderr)
    observed = json.loads(report.read_text()) if report.exists() else {"tests": {}, "exit_code": 1}
    results = observed["tests"]
    concurrency = evaluate(CONCURRENCY, results)
    acceptance = evaluate(ACCEPTANCE, results)
    transport = evaluate(TRANSPORT, results)
    current_paths = git(ROOT, "ls-files", "--cached", "--others", "--exclude-standard").splitlines()
    after = source_snapshot(ROOT, current_paths)
    drift = sorted(
        path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
    )
    passed = (
        completed.returncode == 0
        and observed["exit_code"] == 0
        and not drift
        and all(
            value["result"] == "PASS"
            for value in (*concurrency.values(), *acceptance.values(), *transport.values())
        )
    )
    acceptance["AC26"]["result"] = "PENDING_EXTERNAL_GATE"
    acceptance["AC26"]["external_requirements"] = [
        "exact implementation-head NXS CI and NXS Security",
        "current clean-room certificate",
    ]
    payload: dict[str, Any] = {
        "schema_version": "1.0.0",
        "phase": "NXS-P19",
        "result": "INCOMPLETE",
        "local_test_mapping_result": "PASS" if passed else "FAIL",
        "recorded_at": iso_now(),
        "base_commit": git(ROOT, "rev-parse", "HEAD"),
        "binding": "Current worktree hashes; no implementation commit or closure claim",
        "command": ["uv", "run", *command],
        "exit_code": completed.returncode,
        "runtime_seconds": elapsed,
        "passed": sum(value == "PASS" for value in results.values()),
        "failed": sum(value == "FAIL" for value in results.values()),
        "skipped": sum(value == "SKIP" for value in results.values()),
        "coverage": json.loads(coverage_path.read_text())["totals"]
        if coverage_path.exists()
        else {},
        "concurrency": concurrency,
        "acceptance": acceptance,
        "transport": transport,
        "source_sha256": before,
        "source_drift": drift,
        "closure": "NOT_AUTHORIZED",
    }
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "execution-criteria.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )
    print("P19 local test mapping: " + payload["local_test_mapping_result"])
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
