"""Deterministic repository-content checks for H0-02 GitHub governance."""

from __future__ import annotations

from pathlib import Path

from scripts.nxs_control.core import load_json, validate_all_schemas

ROOT = Path(__file__).parents[2]


def test_github_governance_policy_is_schema_valid_and_registered() -> None:
    validate_all_schemas(ROOT)
    policy = load_json(ROOT / ".nxs/github-governance-policy.json")
    assert policy["target_branch"] == "main"
    assert policy["verification"]["live_application_required"] is True
    assert policy["verification"]["status"] == "VERIFIED"


def test_github_governance_required_checks_are_exact_and_fail_closed() -> None:
    policy = load_json(ROOT / ".nxs/github-governance-policy.json")
    expected = {
        "state",
        "quality",
        "integration",
        "docker",
        "multiarch",
        "source-and-dependencies",
        "image",
    }
    enforcement = policy["expected_enforcement"]
    assert set(enforcement["required_status_checks"]) == expected
    assert enforcement["require_pull_request"] is True
    assert enforcement["strict_status_checks"] is True
    assert enforcement["require_conversation_resolution"] is True
    assert enforcement["block_force_pushes"] is True
    assert enforcement["block_deletions"] is True
    assert policy["verification"]["failure_semantics"] == (
        "UNKNOWN_OR_MISMATCH_BLOCKS_H0_02_COMPLETION"
    )


def test_transitional_review_model_does_not_fabricate_second_reviewer() -> None:
    policy = load_json(ROOT / ".nxs/github-governance-policy.json")
    enforcement = policy["expected_enforcement"]
    assert policy["review_model"] == "SINGLE_MAINTAINER_TRANSITIONAL"
    assert enforcement["required_approving_reviews"] == 0
    assert enforcement["dismiss_stale_approvals"] is False
    assert enforcement["require_linear_history"] is False
    assert enforcement["require_signed_commits"] is False
    assert enforcement["bypass_policy"] == "ADMIN_EMERGENCY_ONLY"


def test_codeowners_covers_control_and_security_surfaces() -> None:
    content = (ROOT / ".github/CODEOWNERS").read_text(encoding="utf-8")
    required_patterns = {
        "* @devopssolutionsia",
        "/.github/ @devopssolutionsia",
        "/.nxs/ @devopssolutionsia",
        "/scripts/nxs_control/ @devopssolutionsia",
        "/scripts/nxs_guard/ @devopssolutionsia",
        "/migrations/ @devopssolutionsia",
        "/infrastructure/ @devopssolutionsia",
        "/src/nexus_ai/domain/auth/ @devopssolutionsia",
        "/src/nexus_ai/audit/ @devopssolutionsia",
        "/src/nexus_ai/sentinel/ @devopssolutionsia",
        "/src/nexus_ai/sip_edge/ @devopssolutionsia",
    }
    assert required_patterns <= set(content.splitlines())
