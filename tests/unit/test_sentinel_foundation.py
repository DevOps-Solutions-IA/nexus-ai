"""Foundation bounds and policy, without external action execution."""

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid7

import pytest
from pydantic import SecretStr, ValidationError

from nexus_ai.core.config import Settings
from nexus_ai.sentinel.config import SentinelSettings
from nexus_ai.sentinel.contracts import (
    Approval,
    IncidentState,
    Parameters,
    Proposal,
    Risk,
    Runbook,
    approval_current,
    canonical_json,
    digest,
    legal_transition,
    proposal_fingerprint,
)
from nexus_ai.sentinel.database import SentinelDatabase
from nexus_ai.sentinel.errors import SentinelDenied
from nexus_ai.sentinel.policy import risk_allowed


def book(risk: Risk = Risk.OBSERVE, **overrides: object) -> Runbook:
    return Runbook.model_validate(
        {
            "id": uuid7(),
            "key": f"test-{uuid7()}",
            "revision": 1,
            "handler_key": "foundation.observe",
            "risk": risk,
            "target_kinds": ["SERVICE"],
            "parameter_schema_digest": digest(Parameters.model_json_schema()),
            "non_mutating": risk in {Risk.OBSERVE, Risk.DIAGNOSTIC},
            "timeout_seconds": 10,
            "retries": 0,
            "policy_requirement": "READ_ONLY"
            if risk in {Risk.OBSERVE, Risk.DIAGNOSTIC}
            else "HUMAN_APPROVAL",
            "idempotency_semantics": "STABLE_OPERATION_ID",
            **overrides,
        }
    )


def proposal(**overrides: object) -> Proposal:
    return Proposal.model_validate(
        {
            "incident_id": uuid7(),
            "incident_revision": 1,
            "runbook_id": uuid7(),
            "runbook_revision": 1,
            "target_kind": "SERVICE",
            "target_id": uuid7(),
            "target_generation": 1,
            "parameters": {},
            "risk": "OBSERVE",
            "policy_revision": 1,
            "operation_identity": uuid7(),
            "creator_type": "SYSTEM",
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "field",
    [
        "pool_size",
        "database_timeout_seconds",
        "adapter_timeout_seconds",
        "model_timeout_seconds",
        "execution_timeout_seconds",
        "signal_batch_size",
        "max_signal_bytes",
        "max_facts_bytes",
        "max_evidence_refs",
        "max_evidence_snippet_bytes",
        "max_incidents_scanned",
        "max_findings_per_incident",
        "max_proposals_per_incident",
        "max_active_executions",
        "lease_duration_seconds",
        "cleanup_batch_size",
    ],
)
@pytest.mark.parametrize("value", [-1, 0, float("inf"), 10**10])
def test_resource_bounds(field, value):
    with pytest.raises(ValidationError):
        SentinelSettings.model_validate({field: value})


@pytest.mark.parametrize(
    "values",
    [
        {"enabled": True},
        {"expected_role": "nexus_runtime"},
        {"reasoning_enabled": "yes"},
        {"safe_read_only_retries": -1},
        {"safe_read_only_retries": 4},
        {"max_signal_bytes": 1024, "max_facts_bytes": 2048},
        {"lease_duration_seconds": 10},
        {"arbitrary_option": 1},
        {"database_dsn": "postgresql://nexus_runtime:secret@localhost/db"},
        {"database_dsn": "postgresql://nexus_sentinel:secret@localhost/db?options=unsafe"},
        {"database_dsn": "http://nexus_sentinel:secret@localhost/db"},
    ],
)
def test_unsafe_config_rejected(values):
    with pytest.raises(ValidationError) as error:
        SentinelSettings.model_validate(values)
    assert "secret@" not in str(error.value)


def test_settings_secret_redaction_and_no_fallback():
    sentinel = SentinelSettings(
        enabled=True,
        database_dsn=SecretStr("postgresql://nexus_sentinel:never-print-this@localhost/db"),
    )
    settings = Settings(sentinel=sentinel)
    assert "never-print-this" not in repr(settings)
    assert "never-print-this" not in settings.model_dump_json()
    with pytest.raises(SentinelDenied):
        SentinelDatabase(SentinelSettings())


@pytest.mark.parametrize("risk", list(Risk))
@pytest.mark.parametrize("approval", [False, True])
@pytest.mark.parametrize("mutable", [False, True])
def test_hardened_risk_matrix(risk, approval, mutable):
    expected = risk in {Risk.OBSERVE, Risk.DIAGNOSTIC} or (
        risk == Risk.REVERSIBLE and approval and mutable
    )
    assert (
        risk_allowed(book(risk), durable_approval=approval, mutable_actions_enabled=mutable)
        is expected
    )


def test_read_only_claim_cannot_hide_mutation():
    for risk in (Risk.OBSERVE, Risk.DIAGNOSTIC):
        assert not risk_allowed(
            book(risk, non_mutating=False), durable_approval=True, mutable_actions_enabled=True
        )
    assert not risk_allowed(
        book(enabled=False), durable_approval=True, mutable_actions_enabled=True
    )


@pytest.mark.parametrize(
    "values",
    [
        {"shell": "sh -c anything"},
        {"sql": "SELECT secret"},
        {"url": "https://evil.test"},
        {"handler_key": "https://evil.test"},
        {"handler_key": "sh -c command"},
        {"raw_credentials": "secret"},
        {"ssh": "server"},
        {"retries": 999},
        {"target_kinds": []},
        {"parameter_schema_digest": "bad"},
    ],
)
def test_runbook_rejects_executable_or_unbounded_data(values):
    with pytest.raises(ValidationError):
        book(**values)


def test_canonical_fingerprint_stable():
    original = proposal()
    reordered = dict(reversed(list(original.model_dump(mode="json").items())))
    assert proposal_fingerprint(original) == proposal_fingerprint(
        Proposal.model_validate(reordered)
    )
    assert canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'
    with pytest.raises(ValueError):
        digest({"bad": float("nan")})
    equivalent = Proposal.model_validate(
        {
            **original.model_dump(),
            "expires_at": original.expires_at.astimezone(timezone(timedelta(hours=-5))),
        }
    )
    assert proposal_fingerprint(equivalent) == proposal_fingerprint(original)


@pytest.mark.parametrize(
    "field,value",
    [
        ("incident_id", uuid7()),
        ("incident_revision", 2),
        ("runbook_id", uuid7()),
        ("runbook_revision", 2),
        ("target_kind", "CELL"),
        ("target_id", uuid7()),
        ("target_generation", 2),
        ("parameters", {"sample_limit": 2}),
        ("risk", "REVERSIBLE"),
        ("policy_revision", 2),
        ("operation_identity", uuid7()),
    ],
)
def test_semantic_delta_changes_fingerprint(field, value):
    original = proposal()
    changed = Proposal.model_validate({**original.model_dump(), field: value})
    assert proposal_fingerprint(changed) != proposal_fingerprint(original)


@pytest.mark.parametrize("before", list(IncidentState))
@pytest.mark.parametrize("after", list(IncidentState))
def test_incident_transition_matrix(before, after):
    states = list(IncidentState)
    expected = states.index(after) == states.index(before) + 1
    source = "TRUSTED_RECOVERY" if after == IncidentState.RESOLVED else None
    assert legal_transition(before, after, source) == expected


def test_resolution_and_approval_not_time_or_model_authority():
    assert not legal_transition(IncidentState.MONITORING, IncidentState.RESOLVED, None)
    assert not legal_transition(IncidentState.MONITORING, IncidentState.RESOLVED, "MODEL")
    original = proposal()
    now = datetime.now(UTC)
    approval = Approval(
        proposal_id=uuid7(),
        proposal_fingerprint=proposal_fingerprint(original),
        approver_principal=uuid7(),
        decision="APPROVED",
        reason_code="reviewed",
        policy_revision=1,
        expires_at=now + timedelta(minutes=1),
    )
    assert approval_current(approval, original, now)
    assert not approval_current(approval, original, now + timedelta(minutes=2))
    assert not approval_current(approval, original.model_copy(update={"policy_revision": 2}), now)
