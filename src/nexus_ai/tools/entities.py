"""Tool Engine domain values and API contracts (NXS-TOOL-001).

A :class:`ToolDefinition` is a deterministic, versioned, tenant-owned facade over exactly
one registered Integration Hub operation. It carries a stable ``tool_key``, an input /
output JSON Schema, the permissions the caller must hold, a risk class, a side-effect
class, an idempotency policy, a timeout and the integration binding.

The invocation contract (:class:`ToolInvocation`) is deliberately minimal: a caller (an
LLM, the future Agent Runtime, the future Workflow Engine) chooses a ``tool_key`` and
validated ``arguments`` — never an ``integration_id``, a URL, an HTTP method, a header
set, an ``operation_key`` or a GraphQL document. Nothing here carries secret material.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

ToolKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{1,62}$")]
OperationKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{1,62}$")]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=800)]
IdempotencyKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{8,200}$")]
PermissionName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9:_-]{2,63}$")]


class RiskClass(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class SideEffectClass(StrEnum):
    READ_ONLY = "READ_ONLY"
    IDEMPOTENT_WRITE = "IDEMPOTENT_WRITE"
    NON_IDEMPOTENT_WRITE = "NON_IDEMPOTENT_WRITE"
    EXTERNAL_EFFECT = "EXTERNAL_EFFECT"


class ToolIdempotencyPolicy(StrEnum):
    NONE = "NONE"
    OPTIONAL = "OPTIONAL"
    REQUIRED = "REQUIRED"


class ToolStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    ERROR = "ERROR"


class ToolBindingType(StrEnum):
    #: The only binding kind in P08 — every tool routes through the Integration Hub.
    INTEGRATION = "INTEGRATION"


class ToolResultClass(StrEnum):
    SUCCESS = "SUCCESS"
    ARGS_INVALID = "ARGS_INVALID"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    POLICY_DENIED = "POLICY_DENIED"
    DISABLED = "DISABLED"
    NOT_FOUND = "NOT_FOUND"
    BINDING_INVALID = "BINDING_INVALID"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    IDEMPOTENCY_REQUIRED = "IDEMPOTENCY_REQUIRED"
    DOWNSTREAM_ERROR = "DOWNSTREAM_ERROR"
    RESULT_INVALID = "RESULT_INVALID"
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    EXECUTION_FAILED = "EXECUTION_FAILED"


#: side-effect classes that MUST carry an idempotency key when the policy is REQUIRED.
SIDE_EFFECTING = frozenset({SideEffectClass.NON_IDEMPOTENT_WRITE, SideEffectClass.EXTERNAL_EFFECT})


class ToolBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    binding_type: ToolBindingType = ToolBindingType.INTEGRATION
    integration_id: UUID
    operation_key: OperationKey


class ToolDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    tool_key: str
    name: str
    description: str | None
    version: int
    status: ToolStatus
    risk_class: RiskClass
    side_effect_class: SideEffectClass
    idempotency_policy: ToolIdempotencyPolicy
    timeout_seconds: float | None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    required_permissions: tuple[str, ...]
    binding: ToolBinding
    #: Fixed arguments merged over the caller's arguments (caller can never override).
    static_arguments: dict[str, Any]
    created_at: dt.datetime
    updated_at: dt.datetime

    def public_view(self) -> ToolDefinitionView:
        return ToolDefinitionView(
            id=self.id,
            organization_id=self.organization_id,
            tool_key=self.tool_key,
            name=self.name,
            description=self.description,
            version=self.version,
            status=self.status,
            risk_class=self.risk_class,
            side_effect_class=self.side_effect_class,
            idempotency_policy=self.idempotency_policy,
            timeout_seconds=self.timeout_seconds,
            input_schema=self.input_schema,
            output_schema=self.output_schema,
            required_permissions=list(self.required_permissions),
            binding_type=self.binding.binding_type,
            integration_id=self.binding.integration_id,
            operation_key=self.binding.operation_key,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


# --- invocation contract -----------------------------------------------------------


class ToolInvocation(BaseModel):
    """The one way to invoke a tool. No integration id, URL, method, headers or
    operation key — a caller chooses a registered ``tool_key`` and validated
    ``arguments`` only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_key: ToolKey
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: IdempotencyKey | None = None
    correlation_id: Annotated[str, StringConstraints(max_length=128)] | None = None


class ToolResult(BaseModel):
    """The normalized outcome of an invocation. Returned for a success and — where the
    engine surfaces rather than raises — for a handled failure."""

    model_config = ConfigDict(frozen=True)

    tool_key: str
    tool_version: int
    result_class: ToolResultClass
    ok: bool
    status_code: int
    output: Any | None = None
    error_code: str | None = None
    error_detail: str | None = None
    downstream_code: str | None = None
    downstream_status: int | None = None
    retry_count: int = 0
    duration_ms: int = 0
    correlation_id: str | None = None
    idempotency_key: str | None = None
    replayed: bool = False


# --- API request / view models -------------------------------------------------


class RegisterToolRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_key: ToolKey
    name: Name
    description: Description | None = None
    risk_class: RiskClass = RiskClass.MEDIUM
    side_effect_class: SideEffectClass = SideEffectClass.READ_ONLY
    idempotency_policy: ToolIdempotencyPolicy = ToolIdempotencyPolicy.OPTIONAL
    timeout_seconds: Annotated[float, Field(gt=0, le=120)] | None = None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    required_permissions: tuple[PermissionName, ...] = ()
    binding: ToolBinding

    @model_validator(mode="after")
    def _coherent(self) -> RegisterToolRequest:
        if (
            self.side_effect_class in SIDE_EFFECTING
            and self.idempotency_policy is ToolIdempotencyPolicy.NONE
        ):
            raise ValueError(
                "a NON_IDEMPOTENT_WRITE / EXTERNAL_EFFECT tool must allow an idempotency key"
            )
        return self


class UpdateToolRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Name | None = None
    description: Description | None = None
    risk_class: RiskClass | None = None
    side_effect_class: SideEffectClass | None = None
    idempotency_policy: ToolIdempotencyPolicy | None = None
    timeout_seconds: Annotated[float, Field(gt=0, le=120)] | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    required_permissions: tuple[PermissionName, ...] | None = None
    binding: ToolBinding | None = None


class ToolDefinitionView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    tool_key: str
    name: str
    description: str | None
    version: int
    status: ToolStatus
    risk_class: RiskClass
    side_effect_class: SideEffectClass
    idempotency_policy: ToolIdempotencyPolicy
    timeout_seconds: float | None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    required_permissions: list[str]
    binding_type: ToolBindingType
    integration_id: UUID
    operation_key: str
    created_at: dt.datetime
    updated_at: dt.datetime
