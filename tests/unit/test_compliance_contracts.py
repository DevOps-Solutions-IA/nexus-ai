"""Bounded schemas, deterministic policy evaluation and lifecycle transitions."""

import datetime as dt
import uuid

import pytest
from pydantic import ValidationError

from nexus_ai.compliance.contracts import (
    Action,
    ApprovalInput,
    ClaimInput,
    ComplianceConflict,
    HoldInput,
    PolicyInput,
    RequestState,
    RetentionRule,
    SubjectRequestInput,
    fingerprint,
    require_transition,
    retention_due,
)


@pytest.mark.parametrize(
    "selector",
    [
        "customers",
        "DROP TABLE customers",
        "$(id)",
        "https://evil.test",
        "IGNORE ALL INSTRUCTIONS",
        "CREDENTIALS",
    ],
)
def test_untrusted_resource_cannot_select_executable_handler(selector):
    with pytest.raises(ValidationError):
        RetentionRule.model_validate({"resource_class": selector, "days": 1, "action": "DELETE"})


@pytest.mark.parametrize("days", [0, -1, 36501, float("inf")])
def test_retention_bounds(days):
    with pytest.raises(ValidationError):
        RetentionRule.model_validate(
            {"resource_class": "CUSTOMER_PROFILE", "days": days, "action": "RETAIN"}
        )


def test_policy_rules_bounded_unique_and_strict():
    rule = {"resource_class": "CUSTOMER_PROFILE", "days": 1, "action": "ANONYMIZE"}
    for rules in ([], [rule, rule], [rule] * 17):
        with pytest.raises(ValidationError):
            PolicyInput.model_validate({"rules": rules})
    with pytest.raises(ValidationError):
        PolicyInput.model_validate({"rules": [rule], "organization_id": str(uuid.uuid4())})


@pytest.mark.parametrize(
    "field",
    ["organization_id", "approver_id", "policy_revision", "handler", "url", "sql", "credential"],
)
def test_subject_request_forbids_authority_fields(field):
    with pytest.raises(ValidationError):
        SubjectRequestInput.model_validate(
            {
                "subject_id": str(uuid.uuid4()),
                "kind": "ACCESS",
                "idempotency_key": "request",
                field: "forged",
            }
        )


def test_fingerprint_semantics_and_budget():
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})
    assert fingerprint({"revision": 1}) != fingerprint({"revision": 2})
    with pytest.raises(ComplianceConflict):
        fingerprint({"oversized": "x" * 20000})
    with pytest.raises(ValueError):
        fingerprint({"invalid": float("nan")})


def test_retention_uses_aware_effective_time():
    now = dt.datetime.now(dt.UTC)
    rule = RetentionRule.model_validate(
        {"resource_class": "CUSTOMER_PROFILE", "days": 1, "action": "ANONYMIZE"}
    )
    assert retention_due(now, now, rule) == Action.RETAIN
    assert retention_due(now - dt.timedelta(days=1), now, rule) == Action.ANONYMIZE
    with pytest.raises(ComplianceConflict):
        retention_due(now.replace(tzinfo=None), now, rule)


@pytest.mark.parametrize("current", list(RequestState))
def test_no_request_self_transition(current):
    with pytest.raises(ComplianceConflict):
        require_transition(current, current)


@pytest.mark.parametrize("state", ["COMPLETED", "PARTIAL", "DENIED", "CANCELLED", "EXPIRED"])
def test_terminal_states_cannot_reopen(state):
    for target in RequestState:
        with pytest.raises(ComplianceConflict):
            require_transition(state, target)


def test_limits_and_safe_metadata():
    for seconds in (0, -1, 3601):
        with pytest.raises(ValidationError):
            ApprovalInput(reason_code="OK", lifetime_seconds=seconds)
    with pytest.raises(ValidationError):
        ClaimInput(owner_id=uuid.uuid4(), lease_seconds=301)
    with pytest.raises(ValidationError):
        HoldInput(subject_id=uuid.uuid4(), reason_code="x" * 97)
