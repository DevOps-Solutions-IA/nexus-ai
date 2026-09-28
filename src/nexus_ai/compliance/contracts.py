"""Strict bounded compliance contracts and deterministic semantic identities."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexus_ai.core.errors import NxsError


class ComplianceConflict(NxsError):
    code = "NXS_COMPLIANCE_FENCED"
    status = 409
    title = "Compliance operation fenced"


class ResourceClass(StrEnum):
    CUSTOMER_PROFILE = "CUSTOMER_PROFILE"
    CUSTOMER_IDENTITIES = "CUSTOMER_IDENTITIES"
    CONVERSATIONS = "CONVERSATIONS"
    MESSAGING = "MESSAGING"
    TELEPHONY = "TELEPHONY"
    AGENT_RUNTIME = "AGENT_RUNTIME"
    WORKFLOWS = "WORKFLOWS"
    CAMPAIGNS = "CAMPAIGNS"
    HUMAN_OPERATIONS = "HUMAN_OPERATIONS"
    EVENTS = "EVENTS"
    CREDENTIALS = "CREDENTIALS"


class Action(StrEnum):
    RETAIN = "RETAIN"
    ACCESS = "ACCESS"
    ANONYMIZE = "ANONYMIZE"
    RESTRICT = "RESTRICT"
    DELETE = "DELETE"


class Classification(StrEnum):
    ORDINARY = "ORDINARY"
    PERSONAL = "PERSONAL"
    CREDENTIAL = "CREDENTIAL"
    OPERATIONAL = "OPERATIONAL"


def resource_inventory() -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "resource_class": resource.value,
            "classification": (
                Classification.CREDENTIAL.value
                if resource == ResourceClass.CREDENTIALS
                else Classification.OPERATIONAL.value
                if resource == ResourceClass.EVENTS
                else Classification.ORDINARY.value
                if resource == ResourceClass.WORKFLOWS
                else Classification.PERSONAL.value
            ),
            "adapter_status": (
                "SUPPORTED" if resource == ResourceClass.CUSTOMER_PROFILE else "DEFERRED"
            ),
        }
        for resource in ResourceClass
    )


class RequestKind(StrEnum):
    ACCESS = "ACCESS"
    ERASURE = "ERASURE"
    RESTRICTION = "RESTRICTION"


class RequestState(StrEnum):
    RECEIVED = "RECEIVED"
    VERIFIED = "VERIFIED"
    PLANNED = "PLANNED"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    DENIED = "DENIED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Code = Annotated[str, Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9_.:-]+$")]


class RetentionRule(StrictModel):
    resource_class: ResourceClass
    days: int = Field(ge=1, le=36500)
    action: Action

    @model_validator(mode="after")
    def supported_action(self) -> RetentionRule:
        if self.action not in {Action.RETAIN, Action.DELETE, Action.ANONYMIZE}:
            raise ValueError("not a retention action")
        if self.action != Action.RETAIN and (
            self.resource_class != ResourceClass.CUSTOMER_PROFILE or self.action != Action.ANONYMIZE
        ):
            raise ValueError("unsupported destructive retention adapter")
        return self


class PolicyInput(StrictModel):
    rules: tuple[RetentionRule, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def unique_rules(self) -> PolicyInput:
        if len({rule.resource_class for rule in self.rules}) != len(self.rules):
            raise ValueError("duplicate resource rule")
        return self


class SubjectRequestInput(StrictModel):
    subject_id: UUID
    kind: RequestKind
    idempotency_key: Code


class VerificationInput(StrictModel):
    method_code: Code
    evidence_reference: Code


class HoldInput(StrictModel):
    subject_id: UUID
    resource_class: ResourceClass | None = None
    reason_code: Code


class DecisionInput(StrictModel):
    reason_code: Code


class ApprovalInput(DecisionInput):
    lifetime_seconds: int = Field(default=300, ge=1, le=3600)


class ClaimInput(StrictModel):
    owner_id: UUID
    lease_seconds: int = Field(default=30, ge=1, le=300)


class ExecutionFence(StrictModel):
    owner_id: UUID
    generation: int = Field(ge=1)


class RetentionInput(StrictModel):
    subject_id: UUID
    resource_class: ResourceClass = ResourceClass.CUSTOMER_PROFILE
    idempotency_key: Code


def fingerprint(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > 16384:
        raise ComplianceConflict("semantic input exceeds resource budget")
    return hashlib.sha256(encoded).hexdigest()


def retention_due(created_at: dt.datetime, now: dt.datetime, rule: RetentionRule) -> Action:
    if created_at.tzinfo is None or now.tzinfo is None:
        raise ComplianceConflict("timezone-aware timestamps required")
    return rule.action if now >= created_at + dt.timedelta(days=rule.days) else Action.RETAIN


DESTRUCTIVE = frozenset({Action.DELETE, Action.ANONYMIZE, Action.RESTRICT})
REQUEST_ACTION = {
    RequestKind.ACCESS: Action.ACCESS,
    RequestKind.ERASURE: Action.ANONYMIZE,
    RequestKind.RESTRICTION: Action.RESTRICT,
}
INCOMPLETE_RESOURCES = tuple(
    resource.value for resource in ResourceClass if resource != ResourceClass.CUSTOMER_PROFILE
)


def require_transition(current: str, target: str) -> None:
    allowed = {
        "RECEIVED": {"VERIFIED", "DENIED", "CANCELLED", "EXPIRED"},
        "VERIFIED": {"PLANNED", "DENIED", "CANCELLED", "EXPIRED"},
        "PLANNED": {"APPROVED", "DENIED", "CANCELLED", "EXPIRED"},
        "APPROVED": {"EXECUTING", "DENIED", "CANCELLED", "EXPIRED"},
        "EXECUTING": {"COMPLETED", "PARTIAL"},
    }
    if target not in allowed.get(current, set()):
        raise ComplianceConflict("invalid subject request transition")
