"""The one canonical, versioned event envelope (NXS-EVENT-002, NXS-EVENT-008).

Every Nexus AI business event — now and in every future phase — is carried by this
structure. It is immutable, strongly validated and serialisable. Tenant authority comes
from :attr:`EventEnvelope.organization_id`, which is written from trusted server-side
context by the producer and re-checked by the consumer against Row-Level Security — it
is never taken from an untrusted payload field.
"""

from __future__ import annotations

import datetime as dt
import enum
import re
import uuid
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nexus_ai.events.errors import EventContractError, EventTenantScopeError
from nexus_ai.events.subjects import Subject, SubjectScope, build_subject

ENVELOPE_SCHEMA_VERSION: Literal["1.0"] = "1.0"

_EVENT_TYPE = re.compile(
    r"\A[a-z][a-z0-9]*(?:_[a-z0-9]+)*(?:\.[a-z][a-z0-9]*(?:_[a-z0-9]+)*){1,5}\Z"
)
_IDENTIFIER = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}\Z")
_MAX_METADATA_ITEMS = 24
_MAX_METADATA_VALUE = 512


class EventScope(enum.StrEnum):
    TENANT = "tenant"
    GLOBAL = "global"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class EventEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0"] = ENVELOPE_SCHEMA_VERSION
    event_id: uuid.UUID
    event_type: str = Field(min_length=3, max_length=160)
    event_version: int = Field(ge=1, le=1_000)
    occurred_at: dt.datetime
    scope: EventScope
    organization_id: uuid.UUID | None = None
    aggregate_type: str = Field(min_length=1, max_length=64)
    aggregate_id: str = Field(min_length=1, max_length=128)
    correlation_id: str = Field(min_length=1, max_length=128)
    causation_id: str | None = Field(default=None, max_length=128)
    trace_id: str | None = Field(default=None, max_length=128)
    producer: str = Field(min_length=1, max_length=96)
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("event_id")
    @classmethod
    def _event_id_is_uuid7(cls, value: uuid.UUID) -> uuid.UUID:
        if value.version != 7:
            raise ValueError("event_id must be a UUIDv7 (time-ordered, globally unique)")
        return value

    @field_validator("event_type")
    @classmethod
    def _event_type_shape(cls, value: str) -> str:
        if not _EVENT_TYPE.match(value):
            raise ValueError(
                "event_type must be a dotted, lowercase, namespaced identifier "
                "(e.g. 'organizations.created')"
            )
        return value

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_is_utc(cls, value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value.astimezone(dt.UTC)

    @field_validator("aggregate_id", "correlation_id")
    @classmethod
    def _identifier_shape(cls, value: str) -> str:
        if not _IDENTIFIER.match(value):
            raise ValueError("identifier contains unsafe characters")
        return value

    @field_validator("metadata")
    @classmethod
    def _metadata_bounds(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > _MAX_METADATA_ITEMS:
            raise ValueError("metadata has too many entries")
        for key, item in value.items():
            if not _IDENTIFIER.match(key) or len(item) > _MAX_METADATA_VALUE:
                raise ValueError("metadata key or value is out of bounds")
        return value

    @model_validator(mode="after")
    def _scope_consistency(self) -> Self:
        if self.scope is EventScope.TENANT and self.organization_id is None:
            raise ValueError("a tenant-scoped event requires organization_id")
        if self.scope is EventScope.GLOBAL and self.organization_id is not None:
            raise ValueError("a global-scoped event must not carry organization_id")
        return self

    # --- construction ---------------------------------------------------------------

    @classmethod
    def create(
        cls,
        *,
        event_type: str,
        event_version: int,
        aggregate_type: str,
        aggregate_id: str,
        producer: str,
        payload: dict[str, Any],
        organization_id: uuid.UUID | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        trace_id: str | None = None,
        metadata: dict[str, str] | None = None,
        occurred_at: dt.datetime | None = None,
    ) -> Self:
        """Build a new envelope with a fresh UUIDv7 id and a UTC timestamp.

        ``organization_id`` decides the scope: given → TENANT, omitted → GLOBAL.
        """
        event_id = uuid.uuid7()
        scope = EventScope.TENANT if organization_id is not None else EventScope.GLOBAL
        return cls(
            event_id=event_id,
            event_type=event_type,
            event_version=event_version,
            occurred_at=occurred_at or _utcnow(),
            scope=scope,
            organization_id=organization_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            correlation_id=correlation_id or event_id.hex,
            causation_id=causation_id,
            trace_id=trace_id,
            producer=producer,
            payload=payload,
            metadata=dict(metadata or {}),
        )

    # --- serialisation -------------------------------------------------------------

    def to_json(self) -> bytes:
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes | str) -> Self:
        """Parse and fully validate an inbound envelope. Raises :class:`EventContractError`."""
        from pydantic import ValidationError

        try:
            return cls.model_validate_json(raw)
        except ValidationError as exc:
            raise EventContractError(
                "the event envelope failed schema validation",
                extensions={"errors": exc.error_count()},
            ) from exc
        except ValueError as exc:
            raise EventContractError("the event envelope is not valid JSON") from exc

    # --- tenant safety ------------------------------------------------------------

    def require_tenant(self) -> uuid.UUID:
        """The trusted Organization id for a tenant event. Raises for global events."""
        if self.scope is not EventScope.TENANT or self.organization_id is None:
            raise EventTenantScopeError("this operation requires a tenant-scoped event")
        return self.organization_id

    def assert_matches_tenant(self, expected: uuid.UUID) -> None:
        """Fail closed when a caller-supplied Organization id disagrees with the envelope."""
        if self.organization_id != expected:
            raise EventTenantScopeError("event organization_id does not match the bound scope")

    # --- transport helpers -------------------------------------------------------

    @property
    def domain(self) -> str:
        return self.event_type.split(".", 1)[0]

    @property
    def event_name(self) -> str:
        return self.event_type.split(".", 1)[1]

    def subject(self, *, prefix: str, environment: str) -> Subject:
        return build_subject(
            prefix=prefix,
            environment=environment,
            scope=SubjectScope(self.scope.value),
            domain=self.domain,
            event=self.event_name,
        )

    def nats_headers(self) -> dict[str, str]:
        """JetStream headers. ``Nats-Msg-Id`` drives broker-side deduplication."""
        headers = {
            "Nats-Msg-Id": str(self.event_id),
            "Nxs-Event-Type": self.event_type,
            "Nxs-Event-Version": str(self.event_version),
            "Nxs-Correlation-Id": self.correlation_id,
        }
        if self.organization_id is not None:
            headers["Nxs-Organization-Id"] = str(self.organization_id)
        return headers

    def log_fields(self) -> dict[str, str]:
        fields = {
            "event_id": str(self.event_id),
            "event_type": self.event_type,
            "event_version": str(self.event_version),
            "correlation_id": self.correlation_id,
            "producer": self.producer,
            "scope": self.scope.value,
        }
        if self.organization_id is not None:
            fields["organization_id"] = str(self.organization_id)
        if self.causation_id is not None:
            fields["causation_id"] = self.causation_id
        return fields
