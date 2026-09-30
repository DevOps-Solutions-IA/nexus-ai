"""Closed platform fact vocabulary; no tenant attribution or arbitrary metadata."""

import datetime as dt
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializeAsAny,
    ValidationInfo,
    field_validator,
    model_validator,
)

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

LEGACY_DOMAIN = "platform:v1:legacy"


@dataclass(frozen=True)
class ProducerSpecification:
    owning_subsystem: str
    source_role: str
    actions: Mapping[str, str]
    services: frozenset[str]
    human_actor_allowed: bool
    metadata_fields: frozenset[str]
    metadata_model: type[BaseModel]
    durability: Literal["SAME_TRANSACTION"] = "SAME_TRANSACTION"


def integrity_domain(producer: str, target_id: UUID) -> str:
    if producer not in PRODUCERS:
        raise ValueError("unregistered platform producer")
    shard = hashlib.sha256(f"{producer}:{target_id}".encode("ascii")).digest()[0] % 16
    return f"platform:v2:{shard:02x}"


def domain_head_id(domain: str) -> int:
    if domain == LEGACY_DOMAIN:
        return 1
    if domain not in {f"platform:v2:{shard:02x}" for shard in range(16)}:
        raise ValueError("invalid platform integrity domain")
    return int(domain.rsplit(":", 1)[1], 16) + 2


class PlatformAuditActor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    kind: Literal["HUMAN", "SERVICE"]
    user_id: UUID | None = None
    service: str | None = Field(default=None, max_length=64)

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


PRODUCERS = MappingProxyType(
    {
        "sentinel": ProducerSpecification(
            owning_subsystem="sentinel",
            source_role="nexus_sentinel",
            actions=MappingProxyType(ACTIONS),
            services=frozenset(
                {"sentinel-adapter", "sentinel-store", "sentinel-policy", "sentinel-executor"}
            ),
            human_actor_allowed=True,
            metadata_fields=frozenset({"state", "reason_code", "related_id", "version", "enabled"}),
            metadata_model=PlatformAuditMetadata,
        )
    }
)


class PlatformAuditIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    version: Literal[1] = 1
    scope: Literal["PLATFORM"] = "PLATFORM"
    organization_id: Literal[None] = None
    source_id: UUID
    producer: str = Field(default="sentinel", max_length=64)
    action: str = Field(max_length=80)
    target_type: str = Field(max_length=32)
    target_id: UUID
    actor: PlatformAuditActor
    occurred_at: dt.datetime
    outcome: Literal["SUCCESS", "FAILED", "DENIED", "AMBIGUOUS"] = "SUCCESS"
    metadata: SerializeAsAny[BaseModel] = Field(default_factory=PlatformAuditMetadata)
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    request_id: UUID | None = None

    @field_validator("metadata", mode="before")
    @classmethod
    def producer_metadata(cls, value: object, info: ValidationInfo) -> BaseModel:
        specification = PRODUCERS.get(info.data.get("producer", "sentinel"))
        if specification is None:
            raise ValueError("unregistered platform producer")
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        return specification.metadata_model.model_validate(value)

    @field_validator("occurred_at")
    @classmethod
    def utc_time(cls, value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None:
            raise ValueError("timezone required")
        return value.astimezone(dt.UTC)

    @model_validator(mode="after")
    def registered(self) -> Self:
        specification = PRODUCERS.get(self.producer)
        if specification is None or specification.actions.get(self.action) != self.target_type:
            raise ValueError("unregistered platform action/target")
        if (self.actor.kind == "SERVICE" and self.actor.service not in specification.services) or (
            self.actor.kind == "HUMAN" and not specification.human_actor_allowed
        ):
            raise ValueError("unregistered platform actor")
        if self.metadata.model_fields_set - specification.metadata_fields:
            raise ValueError("unregistered platform metadata")
        specification.metadata_model.model_validate(self.metadata.model_dump(mode="json"))
        if len(canonical(self.model_dump(mode="json"))) > 4096:
            raise ValueError("platform intent exceeds budget")
        return self


def semantic_digest(intent: PlatformAuditIntent) -> str:
    return record_digest(intent.model_dump(mode="json"))
