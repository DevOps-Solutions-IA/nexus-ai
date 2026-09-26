"""Strict platform metadata. No executable source or arbitrary endpoints."""

import enum
import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

Key = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.:-]+$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Subject(enum.StrEnum):
    GLOBAL = "GLOBAL"
    SERVICE = "SERVICE"
    CELL = "CELL"
    ORGANIZATION = "ORGANIZATION"


class Risk(enum.StrEnum):
    OBSERVE = "OBSERVE"
    DIAGNOSTIC = "DIAGNOSTIC"
    REVERSIBLE = "REVERSIBLE"
    HIGH_IMPACT = "HIGH_IMPACT"
    DESTRUCTIVE = "DESTRUCTIVE"


class IncidentState(enum.StrEnum):
    OPEN = "OPEN"
    TRIAGED = "TRIAGED"
    MITIGATION_PROPOSED = "MITIGATION_PROPOSED"
    MITIGATING = "MITIGATING"
    MONITORING = "MONITORING"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class StrictModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    @field_validator("observed_at", "expires_at", mode="after", check_fields=False)
    @classmethod
    def canonical_timestamp(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class Facts(StrictModel):
    condition: Literal["HEALTHY", "DEGRADED", "UNAVAILABLE", "UNKNOWN"]
    count: int = Field(default=0, ge=0, le=1_000_000_000)
    latency_ms: float | None = Field(default=None, ge=0, le=3_600_000, allow_inf_nan=False)


class Signal(StrictModel):
    adapter_id: Key
    adapter_revision: int = Field(ge=1, le=2_147_483_647)
    source_kind: Key
    source_identity: Key
    source_observation_id: Key
    observed_at: AwareDatetime
    subject_kind: Subject
    subject_id: UUID
    organization_id: UUID | None = None
    severity: Literal["INFO", "WARNING", "ERROR", "CRITICAL"]
    fingerprint: Digest
    facts: Facts
    evidence_refs: tuple[UUID, ...] = Field(default=(), max_length=32)
    schema_version: Literal[1] = 1


class Parameters(StrictModel):
    sample_limit: int = Field(default=1, ge=1, le=100)
    window_seconds: int = Field(default=30, ge=1, le=300)


class Runbook(StrictModel):
    id: UUID
    key: Key
    revision: int = Field(ge=1, le=2_147_483_647)
    handler_key: Key
    handler_revision: int = Field(default=1, ge=1, le=2_147_483_647)
    handler_binding_digest: Digest = "0" * 64
    risk: Risk
    target_kinds: tuple[Subject, ...] = Field(min_length=1, max_length=4)
    parameter_schema_digest: Digest
    non_mutating: bool
    timeout_seconds: int = Field(ge=1, le=120)
    retries: int = Field(ge=0, le=3)
    policy_requirement: Literal["READ_ONLY", "HUMAN_APPROVAL", "DENIED"]
    idempotency_semantics: Literal["READ_ONLY", "STABLE_OPERATION_ID"]
    preconditions: tuple[Literal["ACTIVE_INCIDENT", "CURRENT_GENERATION"], ...] = Field(
        default=("ACTIVE_INCIDENT",), max_length=2
    )
    postconditions: tuple[Literal["TRUSTED_OBSERVATION"], ...] = Field(
        default=("TRUSTED_OBSERVATION",), max_length=1
    )
    compensation_key: Key | None = None
    enabled: bool = True


class Proposal(StrictModel):
    incident_id: UUID
    incident_revision: int = Field(ge=1)
    runbook_id: UUID
    runbook_revision: int = Field(ge=1)
    target_kind: Subject
    target_id: UUID
    target_generation: int | None = Field(default=None, ge=1)
    parameters: Parameters
    risk: Risk
    policy_revision: int = Field(ge=1)
    operation_identity: UUID
    creator_type: Literal["MODEL", "OPERATOR", "SYSTEM"]
    expires_at: AwareDatetime


class Finding(StrictModel):
    incident_id: UUID
    hypothesis_category: Key
    explanation: str = Field(min_length=1, max_length=2048)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    evidence_refs: tuple[UUID, ...] = Field(min_length=1, max_length=32)
    provider_identity: Key | None = None
    model_identity: Key | None = None
    request_fingerprint: Digest | None = None


class Approval(StrictModel):
    proposal_id: UUID
    proposal_fingerprint: Digest
    approver_principal: UUID
    decision: Literal["APPROVED", "REJECTED"]
    reason_code: Key
    policy_revision: int = Field(ge=1)
    expires_at: AwareDatetime


def canonical_json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def proposal_fingerprint(proposal: Proposal) -> str:
    return digest(proposal.model_dump(mode="json"))


def legal_transition(
    before: IncidentState, after: IncidentState, resolution_source: str | None
) -> bool:
    states = list(IncidentState)
    sequential = before != IncidentState.CLOSED and states.index(after) == states.index(before) + 1
    if after == IncidentState.RESOLVED:
        return sequential and resolution_source in {"TRUSTED_RECOVERY", "AUTHORIZED_OPERATOR"}
    return sequential and resolution_source is None


def approval_current(approval: Approval, proposal: Proposal, now: datetime) -> bool:
    return (
        approval.decision == "APPROVED"
        and approval.proposal_fingerprint == proposal_fingerprint(proposal)
        and approval.policy_revision == proposal.policy_revision
        and now < approval.expires_at <= proposal.expires_at
    )
