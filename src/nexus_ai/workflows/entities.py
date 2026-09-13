"""Tenant-safe Workflow Engine entities and strict API contracts."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from nexus_ai.workflows.errors import WorkflowInvalidDefinitionError
from nexus_ai.workflows.state_machine import (
    WorkflowDefinitionStatus,
    WorkflowRunState,
    WorkflowStepState,
)

WorkflowKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{1,62}$")]
StepKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{0,62}$")]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=800)]
IdempotencyKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{8,200}$")]
CorrelationId = Annotated[str, StringConstraints(min_length=1, max_length=128)]
ContextPath = Annotated[
    str, StringConstraints(pattern=r"^(input|steps)(\.[A-Za-z0-9_-]{1,64}){0,15}$")
]


class WorkflowStepType(StrEnum):
    TOOL = "TOOL"
    AGENT = "AGENT"
    CONDITION = "CONDITION"
    NOOP = "NOOP"


class ConditionOperator(StrEnum):
    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    EXISTS = "exists"
    NOT_EXISTS = "not_exists"
    IN = "in"
    NOT_IN = "not_in"
    GREATER_THAN = "greater_than"
    LESS_THAN = "less_than"


class RetryPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_attempts: Annotated[int, Field(ge=1, le=16)] = 1
    retryable_codes: tuple[
        Annotated[str, StringConstraints(pattern=r"^NXS_[A-Z0-9_]{3,96}$")], ...
    ] = ()


class ToolStepConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["TOOL"] = "TOOL"
    tool_key: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{1,62}$")]
    arguments: dict[str, Any] = Field(default_factory=dict)


class AgentStepConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["AGENT"] = "AGENT"
    agent_id: UUID
    prompt: Annotated[str, StringConstraints(min_length=1, max_length=32_768)]


class ConditionStepConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["CONDITION"] = "CONDITION"
    path: ContextPath
    operator: ConditionOperator
    value: Any | None = None
    then_steps: tuple[StepKey, ...]
    else_steps: tuple[StepKey, ...]

    @model_validator(mode="after")
    def _coherent(self) -> ConditionStepConfig:
        if self.operator in {ConditionOperator.EXISTS, ConditionOperator.NOT_EXISTS}:
            if self.value is not None:
                raise ValueError("exists operators do not accept a value")
        elif self.value is None:
            raise ValueError("this condition operator requires a value")
        if set(self.then_steps) & set(self.else_steps):
            raise ValueError("condition branches must be disjoint")
        return self


class NoopStepConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["NOOP"] = "NOOP"
    output: dict[str, Any] = Field(default_factory=dict)


StepConfig = ToolStepConfig | AgentStepConfig | ConditionStepConfig | NoopStepConfig


class WorkflowStepSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: StepKey
    step_type: WorkflowStepType
    depends_on: tuple[StepKey, ...] = ()
    config: StepConfig = Field(discriminator="kind")
    retry: RetryPolicy = RetryPolicy()

    @model_validator(mode="after")
    def _matching_type(self) -> WorkflowStepSpec:
        if self.step_type.value != self.config.kind:
            raise ValueError("step_type must match config.kind")
        if self.key in self.depends_on:
            raise ValueError("a step cannot depend on itself")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("duplicate dependency")
        return self


class CreateWorkflowRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_key: WorkflowKey
    name: Name
    description: Description | None = None
    steps: tuple[WorkflowStepSpec, ...]


class UpdateWorkflowRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    expected_revision: Annotated[int, Field(ge=1)]
    name: Name | None = None
    description: Description | None = None
    steps: tuple[WorkflowStepSpec, ...] | None = None


class StartWorkflowRunRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_version_id: UUID
    input: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: IdempotencyKey | None = None
    correlation_id: CorrelationId | None = None


class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    workflow_key: str
    name: str
    description: str | None
    status: WorkflowDefinitionStatus
    revision: int
    draft_steps: tuple[WorkflowStepSpec, ...]
    created_at: dt.datetime
    updated_at: dt.datetime


class WorkflowVersion(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    definition_id: UUID
    version_number: int
    content_hash: str
    steps: tuple[WorkflowStepSpec, ...]
    published_at: dt.datetime


class WorkflowRun(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    definition_id: UUID
    workflow_version_id: UUID
    state: WorkflowRunState
    input: dict[str, Any]
    output: dict[str, Any] | None
    idempotency_key: str | None
    request_fingerprint: str
    correlation_id: str | None
    error_code: str | None
    state_version: int
    started_at: dt.datetime | None
    ended_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime


class WorkflowStepRun(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    workflow_run_id: UUID
    version_step_id: UUID
    step_key: str
    step_type: WorkflowStepType
    state: WorkflowStepState
    attempt_count: int
    max_attempts: int
    execution_owner_id: UUID | None
    claim_token: UUID | None
    claimed_at: dt.datetime | None
    lease_expires_at: dt.datetime | None
    next_eligible_at: dt.datetime | None
    input: dict[str, Any] | None
    output: dict[str, Any] | None
    error_code: str | None
    external_reference: str | None
    created_at: dt.datetime
    updated_at: dt.datetime


class WorkflowTransition(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    workflow_run_id: UUID
    step_run_id: UUID | None
    entity_type: str
    entity_id: UUID
    from_state: str | None
    to_state: str
    reason_code: str
    source: str
    correlation_id: str | None
    created_at: dt.datetime


class StepClaim(BaseModel):
    model_config = ConfigDict(frozen=True)

    run: WorkflowRun
    step: WorkflowStepRun
    spec: WorkflowStepSpec
    owner_id: UUID
    claim_token: UUID


def validate_workflow_graph(steps: tuple[WorkflowStepSpec, ...]) -> tuple[str, ...]:
    if not steps:
        raise WorkflowInvalidDefinitionError("a workflow requires at least one step")
    if len(steps) > 256:
        raise WorkflowInvalidDefinitionError("a workflow may contain at most 256 steps")
    keys = [step.key for step in steps]
    if len(keys) != len(set(keys)):
        raise WorkflowInvalidDefinitionError("workflow step keys must be unique")
    known = set(keys)
    for step in steps:
        missing = set(step.depends_on) - known
        if missing:
            raise WorkflowInvalidDefinitionError(
                f"step {step.key!r} has unknown dependencies: {sorted(missing)}"
            )
        if isinstance(step.config, ConditionStepConfig):
            branch_missing = (set(step.config.then_steps) | set(step.config.else_steps)) - known
            if branch_missing:
                raise WorkflowInvalidDefinitionError(
                    f"condition {step.key!r} has unknown branches: {sorted(branch_missing)}"
                )
            for branch in (*step.config.then_steps, *step.config.else_steps):
                target = next(candidate for candidate in steps if candidate.key == branch)
                if step.key not in target.depends_on:
                    raise WorkflowInvalidDefinitionError(
                        f"condition branch {branch!r} must depend on {step.key!r}"
                    )
    indegree = {step.key: len(step.depends_on) for step in steps}
    children: dict[str, list[str]] = {step.key: [] for step in steps}
    for step in steps:
        for dependency in step.depends_on:
            children[dependency].append(step.key)
    ready = sorted(key for key, degree in indegree.items() if degree == 0)
    ordered: list[str] = []
    while ready:
        key = ready.pop(0)
        ordered.append(key)
        for child in sorted(children[key]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort()
    if len(ordered) != len(steps):
        raise WorkflowInvalidDefinitionError("workflow graph contains a cycle")
    return tuple(ordered)
