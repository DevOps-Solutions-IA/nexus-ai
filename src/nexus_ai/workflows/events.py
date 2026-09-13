"""Safe P04 workflow lifecycle event payloads."""

from typing import ClassVar
from uuid import UUID

from nexus_ai.events.registry import EVENT_REGISTRY, EventPayload


class _WorkflowDefinitionEvent(EventPayload):
    definition_id: UUID
    workflow_key: str


@EVENT_REGISTRY.payload_model("workflow.definition.created")
class WorkflowDefinitionCreatedV1(_WorkflowDefinitionEvent):
    EVENT_TYPE: ClassVar[str] = "workflow.definition.created"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("workflow.version.published")
class WorkflowVersionPublishedV1(_WorkflowDefinitionEvent):
    EVENT_TYPE: ClassVar[str] = "workflow.version.published"
    VERSION: ClassVar[int] = 1

    version_id: UUID
    version_number: int


class _WorkflowRunEvent(EventPayload):
    run_id: UUID
    definition_id: UUID
    version_id: UUID
    state: str
    correlation_id: str | None = None


@EVENT_REGISTRY.payload_model("workflow.run.started")
class WorkflowRunStartedV1(_WorkflowRunEvent):
    EVENT_TYPE: ClassVar[str] = "workflow.run.started"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("workflow.run.paused")
class WorkflowRunPausedV1(_WorkflowRunEvent):
    EVENT_TYPE: ClassVar[str] = "workflow.run.paused"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("workflow.run.resumed")
class WorkflowRunResumedV1(_WorkflowRunEvent):
    EVENT_TYPE: ClassVar[str] = "workflow.run.resumed"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("workflow.run.completed")
class WorkflowRunCompletedV1(_WorkflowRunEvent):
    EVENT_TYPE: ClassVar[str] = "workflow.run.completed"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("workflow.run.failed")
class WorkflowRunFailedV1(_WorkflowRunEvent):
    EVENT_TYPE: ClassVar[str] = "workflow.run.failed"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("workflow.run.cancelled")
class WorkflowRunCancelledV1(_WorkflowRunEvent):
    EVENT_TYPE: ClassVar[str] = "workflow.run.cancelled"
    VERSION: ClassVar[int] = 1


class _WorkflowStepEvent(EventPayload):
    run_id: UUID
    step_run_id: UUID
    step_key: str
    step_type: str
    state: str
    attempt: int
    correlation_id: str | None = None


@EVENT_REGISTRY.payload_model("workflow.step.ready")
class WorkflowStepReadyV1(_WorkflowStepEvent):
    EVENT_TYPE: ClassVar[str] = "workflow.step.ready"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("workflow.step.started")
class WorkflowStepStartedV1(_WorkflowStepEvent):
    EVENT_TYPE: ClassVar[str] = "workflow.step.started"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("workflow.step.completed")
class WorkflowStepCompletedV1(_WorkflowStepEvent):
    EVENT_TYPE: ClassVar[str] = "workflow.step.completed"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("workflow.step.failed")
class WorkflowStepFailedV1(_WorkflowStepEvent):
    EVENT_TYPE: ClassVar[str] = "workflow.step.failed"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("workflow.step.skipped")
class WorkflowStepSkippedV1(_WorkflowStepEvent):
    EVENT_TYPE: ClassVar[str] = "workflow.step.skipped"
    VERSION: ClassVar[int] = 1
