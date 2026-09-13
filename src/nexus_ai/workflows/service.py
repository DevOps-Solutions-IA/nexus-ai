"""Durable, multi-worker Workflow Engine orchestration service."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError

from nexus_ai.agents.entities import AgentChannel, StartAgentSessionRequest, SubmitTurnRequest
from nexus_ai.agents.service import AgentService
from nexus_ai.core.context import current_context
from nexus_ai.core.errors import NxsError
from nexus_ai.domain.auth.entities import Principal
from nexus_ai.domain.workflows.repository import WorkflowRepository, _definition, _run
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.publisher import EventPublisher
from nexus_ai.infrastructure.database import Database
from nexus_ai.tools.entities import ToolInvocation
from nexus_ai.tools.registry import ToolRegistry
from nexus_ai.tools.service import ToolEngine
from nexus_ai.workflows.conditions import evaluate_condition
from nexus_ai.workflows.entities import (
    AgentStepConfig,
    ConditionStepConfig,
    CreateWorkflowRequest,
    NoopStepConfig,
    StartWorkflowRunRequest,
    StepClaim,
    ToolStepConfig,
    UpdateWorkflowRequest,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowStepRun,
    WorkflowTransition,
    WorkflowVersion,
    validate_workflow_graph,
)
from nexus_ai.workflows.errors import (
    WorkflowConflictError,
    WorkflowInvalidStateError,
    WorkflowNotFoundError,
    WorkflowRunNotFoundError,
    WorkflowStepFailedError,
    WorkflowVersionNotFoundError,
)
from nexus_ai.workflows.state_machine import WorkflowDefinitionStatus, WorkflowRunState


class WorkflowService:
    """Explicit DI boundary; external effects can only enter P08 or P13."""

    def __init__(
        self,
        database: Database,
        publisher: EventPublisher,
        tool_registry: ToolRegistry,
        tool_engine: ToolEngine,
        agent_service: AgentService,
        *,
        service_name: str,
    ) -> None:
        self._db = database
        self._publisher = publisher
        self._tools = tool_registry
        self._tool_engine = tool_engine
        self._agents = agent_service
        self._service_name = service_name

    async def create_definition(
        self, organization_id: uuid.UUID, request: CreateWorkflowRequest
    ) -> WorkflowDefinition:
        validate_workflow_graph(request.steps)
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = WorkflowRepository(tenant)
            definition = await repo.create_definition(
                workflow_key=request.workflow_key,
                name=request.name,
                description=request.description,
                steps=request.steps,
            )
            await self._event(
                tenant.session,
                organization_id,
                "workflow.definition.created",
                "workflow_definition",
                definition.id,
                {"definition_id": str(definition.id), "workflow_key": definition.workflow_key},
            )
            return definition

    async def get_definition(
        self, organization_id: uuid.UUID, definition_id: uuid.UUID
    ) -> WorkflowDefinition:
        async with self._db.tenant_transaction(organization_id) as tenant:
            row = await WorkflowRepository(tenant).definition(definition_id)
            if row is None:
                raise WorkflowNotFoundError("no such workflow definition")
            return _definition(row)

    async def list_definitions(
        self, organization_id: uuid.UUID, *, limit: int, offset: int = 0
    ) -> list[WorkflowDefinition]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await WorkflowRepository(tenant).list_definitions(limit=limit, offset=offset)

    async def update_definition(
        self, organization_id: uuid.UUID, definition_id: uuid.UUID, request: UpdateWorkflowRequest
    ) -> WorkflowDefinition:
        if request.steps is not None:
            validate_workflow_graph(request.steps)
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = WorkflowRepository(tenant)
            row = await repo.definition(definition_id, for_update=True)
            if row is None:
                raise WorkflowNotFoundError("no such workflow definition")
            if row.status == WorkflowDefinitionStatus.ARCHIVED.value:
                raise WorkflowInvalidStateError("an archived workflow cannot be edited")
            if row.revision != request.expected_revision:
                raise WorkflowConflictError("workflow revision changed; reload before editing")
            return await repo.update_definition(
                row, name=request.name, description=request.description, steps=request.steps
            )

    async def publish(
        self, organization_id: uuid.UUID, definition_id: uuid.UUID
    ) -> WorkflowVersion:
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = WorkflowRepository(tenant)
            row = await repo.definition(definition_id, for_update=True)
            if row is None:
                raise WorkflowNotFoundError("no such workflow definition")
            steps = tuple(item for item in _definition(row).draft_steps)
            order = validate_workflow_graph(steps)
        for step in steps:
            if isinstance(step.config, ToolStepConfig):
                await self._tools.get_by_key(organization_id, step.config.tool_key)
            elif isinstance(step.config, AgentStepConfig):
                await self._agents.get_agent(organization_id, step.config.agent_id)
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = WorkflowRepository(tenant)
            row = await repo.definition(definition_id, for_update=True)
            if row is None:
                raise WorkflowNotFoundError("no such workflow definition")
            if tuple(item.model_dump(mode="json") for item in steps) != tuple(row.draft_steps):
                raise WorkflowConflictError("workflow changed while publication was validated")
            version = await repo.publish(row, order)
            await self._event(
                tenant.session,
                organization_id,
                "workflow.version.published",
                "workflow_definition",
                definition_id,
                {
                    "definition_id": str(definition_id),
                    "workflow_key": row.workflow_key,
                    "version_id": str(version.id),
                    "version_number": version.version_number,
                },
            )
            return version

    async def get_version(
        self, organization_id: uuid.UUID, version_id: uuid.UUID
    ) -> WorkflowVersion:
        async with self._db.tenant_transaction(organization_id) as tenant:
            version = await WorkflowRepository(tenant).version(version_id)
            if version is None:
                raise WorkflowVersionNotFoundError("no such workflow version")
            return version

    async def list_versions(
        self, organization_id: uuid.UUID, definition_id: uuid.UUID, *, limit: int
    ) -> list[WorkflowVersion]:
        await self.get_definition(organization_id, definition_id)
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await WorkflowRepository(tenant).list_versions(definition_id, limit=limit)

    async def start_run(
        self, organization_id: uuid.UUID, request: StartWorkflowRunRequest
    ) -> WorkflowRun:
        version = await self.get_version(organization_id, request.workflow_version_id)
        fingerprint = self._fingerprint({"version": str(version.id), "input": request.input})
        try:
            async with self._db.tenant_transaction(organization_id) as tenant:
                repo = WorkflowRepository(tenant)
                run, created = await repo.start_run(
                    version=version,
                    input_payload=request.input,
                    idempotency_key=request.idempotency_key,
                    request_fingerprint=fingerprint,
                    correlation_id=request.correlation_id,
                )
                if not created and run.request_fingerprint != fingerprint:
                    raise WorkflowConflictError("idempotency key was used with a different request")
                if created:
                    await self._event(
                        tenant.session,
                        organization_id,
                        "workflow.run.started",
                        "workflow_run",
                        run.id,
                        self._run_payload(run),
                    )
                    for step in await repo.list_steps(run.id):
                        if step.state.value == "READY":
                            await self._event(
                                tenant.session,
                                organization_id,
                                "workflow.step.ready",
                                "workflow_step_run",
                                step.id,
                                self._step_run_payload(run, step),
                            )
                return run
        except IntegrityError:
            if request.idempotency_key is None:
                raise
            async with self._db.tenant_transaction(organization_id) as tenant:
                resolved_run = await WorkflowRepository(tenant).run_by_idempotency(
                    version.id, request.idempotency_key
                )
                if resolved_run is not None:
                    if resolved_run.request_fingerprint != fingerprint:
                        raise WorkflowConflictError(
                            "idempotency key was used with a different request"
                        ) from None
                    return resolved_run
            raise WorkflowConflictError(
                "the idempotent workflow run could not be resolved after contention"
            ) from None

    async def get_run(self, organization_id: uuid.UUID, run_id: uuid.UUID) -> WorkflowRun:
        async with self._db.tenant_transaction(organization_id) as tenant:
            row = await WorkflowRepository(tenant).run_row(run_id)
            if row is None:
                raise WorkflowRunNotFoundError("no such workflow run")
            return _run(row)

    async def list_runs(
        self, organization_id: uuid.UUID, *, limit: int, offset: int = 0
    ) -> list[WorkflowRun]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await WorkflowRepository(tenant).list_runs(limit=limit, offset=offset)

    async def list_steps(
        self, organization_id: uuid.UUID, run_id: uuid.UUID
    ) -> list[WorkflowStepRun]:
        await self.get_run(organization_id, run_id)
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await WorkflowRepository(tenant).list_steps(run_id)

    async def transitions(
        self, organization_id: uuid.UUID, run_id: uuid.UUID, *, limit: int
    ) -> list[WorkflowTransition]:
        await self.get_run(organization_id, run_id)
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await WorkflowRepository(tenant).transitions(run_id, limit=limit)

    async def pause_run(self, organization_id: uuid.UUID, run_id: uuid.UUID) -> WorkflowRun:
        return await self._transition(
            organization_id,
            run_id,
            WorkflowRunState.PAUSED,
            "WORKFLOW_PAUSED",
            "workflow.run.paused",
        )

    async def resume_run(self, organization_id: uuid.UUID, run_id: uuid.UUID) -> WorkflowRun:
        return await self._transition(
            organization_id,
            run_id,
            WorkflowRunState.RUNNING,
            "WORKFLOW_RESUMED",
            "workflow.run.resumed",
        )

    async def cancel_run(self, organization_id: uuid.UUID, run_id: uuid.UUID) -> WorkflowRun:
        return await self._transition(
            organization_id,
            run_id,
            WorkflowRunState.CANCELLED,
            "WORKFLOW_CANCELLED",
            "workflow.run.cancelled",
        )

    async def _transition(
        self,
        organization_id: uuid.UUID,
        run_id: uuid.UUID,
        target: WorkflowRunState,
        reason: str,
        event_type: str,
    ) -> WorkflowRun:
        async with self._db.tenant_transaction(organization_id) as tenant:
            run = await WorkflowRepository(tenant).transition_run(run_id, target, reason)
            if run is None:
                raise WorkflowRunNotFoundError("no such workflow run")
            await self._event(
                tenant.session,
                organization_id,
                event_type,
                "workflow_run",
                run.id,
                self._run_payload(run),
            )
            return run

    async def claim_next(
        self, organization_id: uuid.UUID, run_id: uuid.UUID, *, owner_id: uuid.UUID | None = None
    ) -> StepClaim | None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            claim = await WorkflowRepository(tenant).claim(run_id, owner_id or uuid.uuid7())
            if claim is not None:
                await self._event(
                    tenant.session,
                    organization_id,
                    "workflow.step.started",
                    "workflow_step_run",
                    claim.step.id,
                    self._step_payload(claim),
                )
            return claim

    async def execute_next(
        self, principal: Principal, run_id: uuid.UUID, *, owner_id: uuid.UUID | None = None
    ) -> WorkflowRun | None:
        claim = await self.claim_next(principal.organization_id, run_id, owner_id=owner_id)
        if claim is None:
            return None
        try:
            output, reference, skipped = await self._execute(principal, claim)
            async with self._db.tenant_transaction(principal.organization_id) as tenant:
                repo = WorkflowRepository(tenant)
                before = {step.step_key: step.state for step in await repo.list_steps(run_id)}
                if skipped is None:
                    run = await repo.finish_step(claim, output=output, external_reference=reference)
                else:
                    run = await repo.finish_condition(
                        claim, output=output, skipped_step_keys=skipped
                    )
                await self._event(
                    tenant.session,
                    principal.organization_id,
                    "workflow.step.completed",
                    "workflow_step_run",
                    claim.step.id,
                    self._step_payload(claim, state="COMPLETED"),
                )
                after = await repo.list_steps(run_id)
                for step in after:
                    if step.state.value == "READY" and before.get(step.step_key) != step.state:
                        await self._event(
                            tenant.session,
                            principal.organization_id,
                            "workflow.step.ready",
                            "workflow_step_run",
                            step.id,
                            self._step_run_payload(run, step),
                        )
                    if step.state.value == "SKIPPED" and before.get(step.step_key) != step.state:
                        await self._event(
                            tenant.session,
                            principal.organization_id,
                            "workflow.step.skipped",
                            "workflow_step_run",
                            step.id,
                            self._step_run_payload(run, step),
                        )
                if run.state is WorkflowRunState.COMPLETED:
                    await self._event(
                        tenant.session,
                        principal.organization_id,
                        "workflow.run.completed",
                        "workflow_run",
                        run.id,
                        self._run_payload(run),
                    )
                return run
        except NxsError as exc:
            retryable = exc.retryable and exc.code in claim.spec.retry.retryable_codes
            async with self._db.tenant_transaction(principal.organization_id) as tenant:
                run = await WorkflowRepository(tenant).fail_step(
                    claim, error_code=exc.code, retryable=retryable
                )
                current_step = next(
                    step
                    for step in await WorkflowRepository(tenant).list_steps(run_id)
                    if step.id == claim.step.id
                )
                event_type = (
                    "workflow.step.ready"
                    if current_step.state.value == "READY"
                    else "workflow.step.failed"
                )
                await self._event(
                    tenant.session,
                    principal.organization_id,
                    event_type,
                    "workflow_step_run",
                    claim.step.id,
                    self._step_run_payload(run, current_step),
                )
                if run.state is WorkflowRunState.FAILED:
                    await self._event(
                        tenant.session,
                        principal.organization_id,
                        "workflow.run.failed",
                        "workflow_run",
                        run.id,
                        self._run_payload(run),
                    )
            raise WorkflowStepFailedError(
                "workflow step execution failed", extensions={"upstream_code": exc.code}
            ) from exc

    async def _execute(
        self, principal: Principal, claim: StepClaim
    ) -> tuple[dict[str, object], str | None, tuple[str, ...] | None]:
        config = claim.spec.config
        semantic_key = f"wf:{claim.run.id}:{claim.step.step_key}"
        if isinstance(config, ToolStepConfig):
            result = await self._tool_engine.invoke(
                principal,
                ToolInvocation(
                    tool_key=config.tool_key,
                    arguments=config.arguments,
                    idempotency_key=semantic_key,
                    correlation_id=claim.run.correlation_id,
                ),
            )
            if not result.ok:
                raise WorkflowStepFailedError(
                    "tool step returned a failure", extensions={"tool_code": result.error_code}
                )
            value = result.output
            return ({"result": value} if not isinstance(value, dict) else value), None, None
        if isinstance(config, AgentStepConfig):
            session = await self._agents.start_session(
                principal.organization_id,
                principal,
                StartAgentSessionRequest(
                    agent_id=config.agent_id,
                    channel=AgentChannel.API,
                    correlation_id=claim.run.correlation_id,
                    idempotency_key=f"{semantic_key}:session",
                    metadata={"purpose": "workflow"},
                ),
            )
            response = await self._agents.submit_turn(
                principal.organization_id,
                session.id,
                SubmitTurnRequest(
                    content=config.prompt,
                    idempotency_key=f"{semantic_key}:turn",
                    correlation_id=claim.run.correlation_id,
                ),
            )
            return (
                {"content": response.content, "finish_reason": response.finish_reason},
                str(session.id),
                None,
            )
        if isinstance(config, ConditionStepConfig):
            steps = await self.list_steps(principal.organization_id, claim.run.id)
            context: dict[str, Any] = {
                "input": claim.run.input,
                "steps": {step.step_key: step.output for step in steps if step.output is not None},
            }
            selected = evaluate_condition(
                config,
                workflow_input=claim.run.input,
                step_outputs=context["steps"],
            )
            skipped = config.else_steps if selected else config.then_steps
            return {"selected": selected}, None, skipped
        if isinstance(config, NoopStepConfig):
            return dict(config.output), None, None
        raise WorkflowStepFailedError("unsupported workflow step type")

    async def _event(
        self,
        session: Any,
        organization_id: uuid.UUID,
        event_type: str,
        aggregate_type: str,
        aggregate_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> None:
        context = current_context()
        envelope = EventEnvelope.create(
            event_type=event_type,
            event_version=1,
            aggregate_type=aggregate_type,
            aggregate_id=str(aggregate_id),
            producer=self._service_name,
            organization_id=organization_id,
            correlation_id=None if context is None else context.correlation_id,
            payload=payload,
        )
        await self._publisher.enqueue(session, envelope)

    @staticmethod
    def _fingerprint(value: dict[str, object]) -> str:
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _run_payload(run: WorkflowRun) -> dict[str, Any]:
        return {
            "run_id": str(run.id),
            "definition_id": str(run.definition_id),
            "version_id": str(run.workflow_version_id),
            "state": run.state.value,
            "correlation_id": run.correlation_id,
        }

    @staticmethod
    def _step_payload(claim: StepClaim, *, state: str = "RUNNING") -> dict[str, Any]:
        return {
            "run_id": str(claim.run.id),
            "step_run_id": str(claim.step.id),
            "step_key": claim.step.step_key,
            "step_type": claim.step.step_type.value,
            "state": state,
            "attempt": claim.step.attempt_count,
            "correlation_id": claim.run.correlation_id,
        }

    @staticmethod
    def _step_run_payload(run: WorkflowRun, step: WorkflowStepRun) -> dict[str, Any]:
        return {
            "run_id": str(run.id),
            "step_run_id": str(step.id),
            "step_key": step.step_key,
            "step_type": step.step_type.value,
            "state": step.state.value,
            "attempt": step.attempt_count,
            "correlation_id": run.correlation_id,
        }
