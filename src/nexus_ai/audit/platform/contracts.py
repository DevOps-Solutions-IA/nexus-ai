"""Closed platform fact vocabulary; no tenant attribution or arbitrary metadata."""

import datetime as dt
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nexus_ai.audit.contracts import canonical, record_digest

ACTIONS = {
    "sentinel.signal.observed": "signal",
    "sentinel.incident.transitioned": "incident",
    "sentinel.finding.recorded": "finding",
    "sentinel.runbook.registered": "runbook",
    "sentinel.proposal.recorded": "proposal",
    "sentinel.approval.recorded": "approval",
    "sentinel.kill_switch.changed": "control",
    **{
        f"sentinel.execution.{action}": "execution"
        for action in (
            "claimed",
            "dispatched",
            "completed",
            "failed",
            "ambiguous",
            "denied",
            "expired",
        )
    },
}


class PlatformAuditActor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    kind: Literal["HUMAN", "SERVICE"]
    user_id: UUID | None = None
    service: (
        Literal["sentinel-adapter", "sentinel-store", "sentinel-policy", "sentinel-executor"] | None
    ) = None

    @model_validator(mode="after")
    def provenance(self) -> Self:
        if (self.kind == "HUMAN" and (self.user_id is None or self.service is not None)) or (
            self.kind == "SERVICE" and (self.service is None or self.user_id is not None)
        ):
            raise ValueError("explicit platform actor provenance required")
        return self


class PlatformAuditMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    state: (
        Literal[
            "OPEN",
            "TRIAGED",
            "MITIGATION_PROPOSED",
            "MONITORING",
            "INVESTIGATING",
            "MITIGATING",
            "RESOLVED",
            "CLOSED",
            "ACKNOWLEDGED",
            "APPROVED",
            "REJECTED",
            "DENIED",
            "CLAIMED",
            "DISPATCHED",
            "SUCCEEDED",
            "FAILED",
            "AMBIGUOUS",
            "EXPIRED",
        ]
        | None
    ) = None
    reason_code: Literal["POLICY_DENIED", "AMBIGUOUS", "EXPIRED"] | None = None
    related_id: UUID | None = None
    version: int | None = Field(default=None, ge=0, le=2_147_483_647, strict=True)
    enabled: bool | None = Field(default=None, strict=True)


class PlatformAuditIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    version: Literal[1] = 1
    scope: Literal["PLATFORM"] = "PLATFORM"
    organization_id: Literal[None] = None
    source_id: UUID
    producer: Literal["sentinel"] = "sentinel"
    action: str = Field(max_length=80)
    target_type: str = Field(max_length=32)
    target_id: UUID
    actor: PlatformAuditActor
    occurred_at: dt.datetime
    outcome: Literal["SUCCESS", "FAILED", "DENIED", "AMBIGUOUS"] = "SUCCESS"
    metadata: PlatformAuditMetadata = Field(default_factory=PlatformAuditMetadata)
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    request_id: UUID | None = None

    @field_validator("occurred_at")
    @classmethod
    def utc_time(cls, value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None:
            raise ValueError("timezone required")
        return value.astimezone(dt.UTC)

    @model_validator(mode="after")
    def registered(self) -> Self:
        if ACTIONS.get(self.action) != self.target_type:
            raise ValueError("unregistered platform action/target")
        if len(canonical(self.model_dump(mode="json"))) > 4096:
            raise ValueError("platform intent exceeds budget")
        return self


def semantic_digest(intent: PlatformAuditIntent) -> str:
    return record_digest(intent.model_dump(mode="json"))
