"""Tenant-scoped workflow repositories and PostgreSQL fencing primitives."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Select, select, update

from nexus_ai.domain.workflows.models import (
    WorkflowDefinitionRecord,
    WorkflowRunRecord,
    WorkflowStepRunRecord,
    WorkflowTransitionHistoryRecord,
    WorkflowVersionRecord,
    WorkflowVersionStepRecord,
)
from nexus_ai.infrastructure.tenant_session import TenantSession
from nexus_ai.workflows.entities import (
    StepClaim,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowStepRun,
    WorkflowStepSpec,
    WorkflowStepType,
    WorkflowTransition,
    WorkflowVersion,
)
from nexus_ai.workflows.errors import WorkflowExecutionFencedError
from nexus_ai.workflows.state_machine import (
    WorkflowDefinitionStatus,
    WorkflowRunState,
    WorkflowStepState,
    require_workflow_transition,
)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _definition(row: WorkflowDefinitionRecord) -> WorkflowDefinition:
    return WorkflowDefinition(
        id=row.id,
        organization_id=row.organization_id,
        workflow_key=row.workflow_key,
        name=row.name,
        description=row.description,
        status=WorkflowDefinitionStatus(row.status),
        revision=row.revision,
        draft_steps=tuple(WorkflowStepSpec.model_validate(item) for item in row.draft_steps),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _run(row: WorkflowRunRecord) -> WorkflowRun:
    return WorkflowRun(
        id=row.id,
        organization_id=row.organization_id,
        definition_id=row.definition_id,
        workflow_version_id=row.version_id,
        state=WorkflowRunState(row.state),
        input=dict(row.input_payload),
        output=None if row.output_payload is None else dict(row.output_payload),
        idempotency_key=row.idempotency_key,
        request_fingerprint=row.request_fingerprint,
        correlation_id=row.correlation_id,
        error_code=row.error_code,
        state_version=row.state_version,
        started_at=row.started_at,
        ended_at=row.ended_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _step(row: WorkflowStepRunRecord) -> WorkflowStepRun:
    return WorkflowStepRun(
        id=row.id,
        organization_id=row.organization_id,
        workflow_run_id=row.run_id,
        version_step_id=row.version_step_id,
        step_key=row.step_key,
        step_type=WorkflowStepType(row.step_type),
        state=WorkflowStepState(row.state),
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        execution_owner_id=row.execution_owner_id,
        claim_token=row.claim_token,
        claimed_at=row.claimed_at,
        lease_expires_at=row.lease_expires_at,
        next_eligible_at=row.next_eligible_at,
        input=dict(row.input_payload),
        output=None if row.output_payload is None else dict(row.output_payload),
        error_code=row.error_code,
        external_reference=row.external_execution_ref,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class WorkflowRepository:
    """All operations are RLS-confined by the supplied tenant transaction."""

    def __init__(self, tenant: TenantSession) -> None:
        self._session = tenant.session
        self._org = tenant.organization_id

    async def create_definition(
        self,
        *,
        workflow_key: str,
        name: str,
        description: str | None,
        steps: tuple[WorkflowStepSpec, ...],
    ) -> WorkflowDefinition:
        row = WorkflowDefinitionRecord(
            id=uuid.uuid7(),
            organization_id=self._org,
            workflow_key=workflow_key,
            name=name,
            description=description,
            status=WorkflowDefinitionStatus.DRAFT.value,
            revision=1,
            draft_steps=[item.model_dump(mode="json") for item in steps],
        )
        self._session.add(row)
        await self._session.flush()
        return _definition(row)

    async def definition(
        self, definition_id: uuid.UUID, *, for_update: bool = False
    ) -> WorkflowDefinitionRecord | None:
        query: Select[tuple[WorkflowDefinitionRecord]] = select(WorkflowDefinitionRecord).where(
            WorkflowDefinitionRecord.organization_id == self._org,
            WorkflowDefinitionRecord.id == definition_id,
        )
        if for_update:
            query = query.with_for_update()
        return (await self._session.execute(query)).scalar_one_or_none()

    async def list_definitions(self, *, limit: int, offset: int = 0) -> list[WorkflowDefinition]:
        rows = (
            (
                await self._session.execute(
                    select(WorkflowDefinitionRecord)
                    .where(WorkflowDefinitionRecord.organization_id == self._org)
                    .order_by(
                        WorkflowDefinitionRecord.created_at.desc(), WorkflowDefinitionRecord.id
                    )
                    .offset(offset)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_definition(row) for row in rows]

    async def update_definition(
        self,
        row: WorkflowDefinitionRecord,
        *,
        name: str | None,
        description: str | None,
        steps: tuple[WorkflowStepSpec, ...] | None,
    ) -> WorkflowDefinition:
        if name is not None:
            row.name = name
        if description is not None:
            row.description = description
        if steps is not None:
            row.draft_steps = [item.model_dump(mode="json") for item in steps]
        row.revision += 1
        row.updated_at = _utcnow()
        await self._session.flush()
        return _definition(row)

    async def publish(
        self, row: WorkflowDefinitionRecord, ordered_keys: tuple[str, ...]
    ) -> WorkflowVersion:
        number = (
            await self._session.execute(
                select(WorkflowVersionRecord.version_number)
                .where(
                    WorkflowVersionRecord.organization_id == self._org,
                    WorkflowVersionRecord.definition_id == row.id,
                )
                .order_by(WorkflowVersionRecord.version_number.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        steps = tuple(WorkflowStepSpec.model_validate(item) for item in row.draft_steps)
        import hashlib
        import json

        canonical = json.dumps(
            [item.model_dump(mode="json") for item in steps], sort_keys=True, separators=(",", ":")
        )
        version = WorkflowVersionRecord(
            id=uuid.uuid7(),
            organization_id=self._org,
            definition_id=row.id,
            version_number=(number or 0) + 1,
            content_hash=hashlib.sha256(canonical.encode()).hexdigest(),
        )
        self._session.add(version)
        await self._session.flush()
        by_key = {item.key: item for item in steps}
        for order, key in enumerate(ordered_keys):
            item = by_key[key]
            self._session.add(
                WorkflowVersionStepRecord(
                    id=uuid.uuid7(),
                    organization_id=self._org,
                    version_id=version.id,
                    step_key=item.key,
                    step_type=item.step_type.value,
                    dependencies=list(item.depends_on),
                    configuration=item.config.model_dump(mode="json"),
                    retry_policy=item.retry.model_dump(mode="json"),
                    topological_order=order,
                )
            )
        row.status = WorkflowDefinitionStatus.ACTIVE.value
        row.updated_at = _utcnow()
        await self._session.flush()
        return WorkflowVersion(
            id=version.id,
            organization_id=self._org,
            definition_id=row.id,
            version_number=version.version_number,
            content_hash=version.content_hash,
            steps=steps,
            published_at=version.published_at,
        )

    async def version(self, version_id: uuid.UUID) -> WorkflowVersion | None:
        row = (
            await self._session.execute(
                select(WorkflowVersionRecord).where(
                    WorkflowVersionRecord.organization_id == self._org,
                    WorkflowVersionRecord.id == version_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        steps = await self._version_specs(row.id)
        return WorkflowVersion(
            id=row.id,
            organization_id=row.organization_id,
            definition_id=row.definition_id,
            version_number=row.version_number,
            content_hash=row.content_hash,
            steps=steps,
            published_at=row.published_at,
        )

    async def list_versions(self, definition_id: uuid.UUID, *, limit: int) -> list[WorkflowVersion]:
        rows = (
            (
                await self._session.execute(
                    select(WorkflowVersionRecord)
                    .where(
                        WorkflowVersionRecord.organization_id == self._org,
                        WorkflowVersionRecord.definition_id == definition_id,
                    )
                    .order_by(WorkflowVersionRecord.version_number.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [
            WorkflowVersion(
                id=row.id,
                organization_id=row.organization_id,
                definition_id=row.definition_id,
                version_number=row.version_number,
                content_hash=row.content_hash,
                steps=await self._version_specs(row.id),
                published_at=row.published_at,
            )
            for row in rows
        ]

    async def _version_specs(self, version_id: uuid.UUID) -> tuple[WorkflowStepSpec, ...]:
        rows = (
            (
                await self._session.execute(
                    select(WorkflowVersionStepRecord)
                    .where(
                        WorkflowVersionStepRecord.organization_id == self._org,
                        WorkflowVersionStepRecord.version_id == version_id,
                    )
                    .order_by(WorkflowVersionStepRecord.topological_order)
                )
            )
            .scalars()
            .all()
        )
        return tuple(
            WorkflowStepSpec.model_validate(
                {
                    "key": row.step_key,
                    "step_type": row.step_type,
                    "depends_on": row.dependencies,
                    "config": row.configuration,
                    "retry": row.retry_policy,
                }
            )
            for row in rows
        )

    async def start_run(
        self,
        *,
        version: WorkflowVersion,
        input_payload: dict[str, object],
        idempotency_key: str | None,
        request_fingerprint: str,
        correlation_id: str | None,
    ) -> tuple[WorkflowRun, bool]:
        if idempotency_key:
            existing = (
                await self._session.execute(
                    select(WorkflowRunRecord).where(
                        WorkflowRunRecord.organization_id == self._org,
                        WorkflowRunRecord.version_id == version.id,
                        WorkflowRunRecord.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return _run(existing), False
        run_row = WorkflowRunRecord(
            id=uuid.uuid7(),
            organization_id=self._org,
            definition_id=version.definition_id,
            version_id=version.id,
            state=WorkflowRunState.PENDING.value,
            state_version=1,
            input_payload=input_payload,
            output_payload=None,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            correlation_id=correlation_id,
            error_code=None,
            terminal_reason=None,
        )
        self._session.add(run_row)
        await self._session.flush()
        await self._transition_run(run_row, WorkflowRunState.RUNNING, "WORKFLOW_STARTED")
        step_ids = await self._version_step_ids(version.id)
        for spec in version.steps:
            initial = WorkflowStepState.READY if not spec.depends_on else WorkflowStepState.PENDING
            step_row = WorkflowStepRunRecord(
                id=uuid.uuid7(),
                organization_id=self._org,
                run_id=run_row.id,
                version_step_id=step_ids[spec.key],
                step_key=spec.key,
                step_type=spec.step_type.value,
                state=initial.value,
                attempt_count=0,
                max_attempts=spec.retry.max_attempts,
                execution_owner_id=None,
                claim_token=None,
                input_payload={},
                output_payload=None,
            )
            self._session.add(step_row)
            await self.add_transition(
                run_row,
                step_row,
                initial.value,
                "INITIAL_READY" if initial is WorkflowStepState.READY else "AWAITING_DEPENDENCIES",
                "ENGINE",
            )
        await self._session.flush()
        return _run(run_row), True

    async def run_by_idempotency(
        self, version_id: uuid.UUID, idempotency_key: str
    ) -> WorkflowRun | None:
        row = (
            await self._session.execute(
                select(WorkflowRunRecord).where(
                    WorkflowRunRecord.organization_id == self._org,
                    WorkflowRunRecord.version_id == version_id,
                    WorkflowRunRecord.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        return _run(row) if row is not None else None

    async def _version_step_ids(self, version_id: uuid.UUID) -> dict[str, uuid.UUID]:
        rows = (
            await self._session.execute(
                select(WorkflowVersionStepRecord.step_key, WorkflowVersionStepRecord.id).where(
                    WorkflowVersionStepRecord.organization_id == self._org,
                    WorkflowVersionStepRecord.version_id == version_id,
                )
            )
        ).all()
        return {str(row.step_key): row.id for row in rows}

    async def run_row(
        self, run_id: uuid.UUID, *, for_update: bool = False
    ) -> WorkflowRunRecord | None:
        query: Select[tuple[WorkflowRunRecord]] = select(WorkflowRunRecord).where(
            WorkflowRunRecord.organization_id == self._org, WorkflowRunRecord.id == run_id
        )
        if for_update:
            query = query.with_for_update()
        return (await self._session.execute(query)).scalar_one_or_none()

    async def list_runs(self, *, limit: int, offset: int = 0) -> list[WorkflowRun]:
        rows = (
            (
                await self._session.execute(
                    select(WorkflowRunRecord)
                    .where(WorkflowRunRecord.organization_id == self._org)
                    .order_by(WorkflowRunRecord.created_at.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_run(row) for row in rows]

    async def list_steps(self, run_id: uuid.UUID) -> list[WorkflowStepRun]:
        rows = (
            (
                await self._session.execute(
                    select(WorkflowStepRunRecord)
                    .where(
                        WorkflowStepRunRecord.organization_id == self._org,
                        WorkflowStepRunRecord.run_id == run_id,
                    )
                    .order_by(WorkflowStepRunRecord.created_at, WorkflowStepRunRecord.step_key)
                )
            )
            .scalars()
            .all()
        )
        return [_step(row) for row in rows]

    async def claim(self, run_id: uuid.UUID, owner_id: uuid.UUID) -> StepClaim | None:
        run_row = await self.run_row(run_id, for_update=True)
        if run_row is None or run_row.state != WorkflowRunState.RUNNING.value:
            return None
        now = _utcnow()
        row = (
            await self._session.execute(
                select(WorkflowStepRunRecord)
                .where(
                    WorkflowStepRunRecord.organization_id == self._org,
                    WorkflowStepRunRecord.run_id == run_id,
                    WorkflowStepRunRecord.state == WorkflowStepState.READY.value,
                    (
                        WorkflowStepRunRecord.next_eligible_at.is_(None)
                        | (WorkflowStepRunRecord.next_eligible_at <= now)
                    ),
                )
                .order_by(WorkflowStepRunRecord.created_at, WorkflowStepRunRecord.step_key)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        token = uuid.uuid4()
        row.state = WorkflowStepState.RUNNING.value
        row.attempt_count += 1
        row.execution_owner_id = owner_id
        row.claim_token = token
        row.claimed_at = now
        row.updated_at = now
        await self.add_transition(
            run_row,
            row,
            WorkflowStepState.RUNNING.value,
            "STEP_CLAIMED",
            "WORKER",
            from_state=WorkflowStepState.READY.value,
        )
        spec = await self._spec_for_step(row.version_step_id)
        await self._session.flush()
        return StepClaim(
            run=_run(run_row), step=_step(row), spec=spec, owner_id=owner_id, claim_token=token
        )

    async def _spec_for_step(self, version_step_id: uuid.UUID) -> WorkflowStepSpec:
        row = (
            await self._session.execute(
                select(WorkflowVersionStepRecord).where(
                    WorkflowVersionStepRecord.organization_id == self._org,
                    WorkflowVersionStepRecord.id == version_step_id,
                )
            )
        ).scalar_one()
        return WorkflowStepSpec.model_validate(
            {
                "key": row.step_key,
                "step_type": row.step_type,
                "depends_on": row.dependencies,
                "config": row.configuration,
                "retry": row.retry_policy,
            }
        )

    async def finish_step(
        self, claim: StepClaim, *, output: dict[str, object], external_reference: str | None = None
    ) -> WorkflowRun:
        run_row = await self.run_row(claim.run.id, for_update=True)
        if run_row is None or run_row.state not in {
            WorkflowRunState.RUNNING.value,
            WorkflowRunState.PAUSED.value,
        }:
            raise WorkflowExecutionFencedError()
        result = await self._session.execute(
            update(WorkflowStepRunRecord)
            .where(
                WorkflowStepRunRecord.organization_id == self._org,
                WorkflowStepRunRecord.id == claim.step.id,
                WorkflowStepRunRecord.state == WorkflowStepState.RUNNING.value,
                WorkflowStepRunRecord.execution_owner_id == claim.owner_id,
                WorkflowStepRunRecord.claim_token == claim.claim_token,
            )
            .values(
                state=WorkflowStepState.COMPLETED.value,
                output_payload=output,
                external_execution_ref=external_reference,
                updated_at=_utcnow(),
            )
            .returning(WorkflowStepRunRecord)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise WorkflowExecutionFencedError()
        await self.add_transition(
            run_row,
            row,
            WorkflowStepState.COMPLETED.value,
            "STEP_COMPLETED",
            "WORKER",
            from_state=WorkflowStepState.RUNNING.value,
        )
        await self._advance(run_row)
        await self._session.flush()
        return _run(run_row)

    async def finish_condition(
        self,
        claim: StepClaim,
        *,
        output: dict[str, object],
        skipped_step_keys: tuple[str, ...],
    ) -> WorkflowRun:
        run_row = await self.run_row(claim.run.id, for_update=True)
        if run_row is None or run_row.state not in {
            WorkflowRunState.RUNNING.value,
            WorkflowRunState.PAUSED.value,
        }:
            raise WorkflowExecutionFencedError()
        row = (
            await self._session.execute(
                select(WorkflowStepRunRecord)
                .where(
                    WorkflowStepRunRecord.organization_id == self._org,
                    WorkflowStepRunRecord.id == claim.step.id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            row is None
            or row.state != WorkflowStepState.RUNNING.value
            or row.execution_owner_id != claim.owner_id
            or row.claim_token != claim.claim_token
        ):
            raise WorkflowExecutionFencedError()
        row.state = WorkflowStepState.COMPLETED.value
        row.output_payload = output
        row.updated_at = _utcnow()
        await self.add_transition(
            run_row,
            row,
            WorkflowStepState.COMPLETED.value,
            "CONDITION_EVALUATED",
            "WORKER",
            from_state=WorkflowStepState.RUNNING.value,
        )
        if skipped_step_keys:
            skipped = (
                (
                    await self._session.execute(
                        select(WorkflowStepRunRecord)
                        .where(
                            WorkflowStepRunRecord.organization_id == self._org,
                            WorkflowStepRunRecord.run_id == run_row.id,
                            WorkflowStepRunRecord.step_key.in_(skipped_step_keys),
                            WorkflowStepRunRecord.state.in_(
                                [WorkflowStepState.PENDING.value, WorkflowStepState.READY.value]
                            ),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            for target in skipped:
                previous = target.state
                target.state = WorkflowStepState.SKIPPED.value
                await self.add_transition(
                    run_row,
                    target,
                    WorkflowStepState.SKIPPED.value,
                    "CONDITION_NOT_SELECTED",
                    "ENGINE",
                    from_state=previous,
                )
        await self._advance(run_row)
        await self._session.flush()
        return _run(run_row)

    async def fail_step(self, claim: StepClaim, *, error_code: str, retryable: bool) -> WorkflowRun:
        run_row = await self.run_row(claim.run.id, for_update=True)
        if run_row is None or run_row.state not in {
            WorkflowRunState.RUNNING.value,
            WorkflowRunState.PAUSED.value,
        }:
            raise WorkflowExecutionFencedError()
        row = (
            await self._session.execute(
                select(WorkflowStepRunRecord)
                .where(
                    WorkflowStepRunRecord.organization_id == self._org,
                    WorkflowStepRunRecord.id == claim.step.id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            row is None
            or row.state != WorkflowStepState.RUNNING.value
            or row.execution_owner_id != claim.owner_id
            or row.claim_token != claim.claim_token
        ):
            raise WorkflowExecutionFencedError()
        if retryable and row.attempt_count < row.max_attempts:
            row.state = WorkflowStepState.READY.value
            row.execution_owner_id = None
            row.claim_token = None
            row.error_code = error_code
            row.updated_at = _utcnow()
            await self.add_transition(
                run_row,
                row,
                WorkflowStepState.READY.value,
                "STEP_RETRY_READY",
                "WORKER",
                from_state=WorkflowStepState.RUNNING.value,
            )
        else:
            row.state = WorkflowStepState.FAILED.value
            row.error_code = error_code
            row.updated_at = _utcnow()
            await self.add_transition(
                run_row,
                row,
                WorkflowStepState.FAILED.value,
                error_code,
                "WORKER",
                from_state=WorkflowStepState.RUNNING.value,
            )
            await self._transition_run(run_row, WorkflowRunState.FAILED, error_code)
        await self._session.flush()
        return _run(run_row)

    async def _advance(self, run_row: WorkflowRunRecord) -> None:
        steps = (
            (
                await self._session.execute(
                    select(WorkflowStepRunRecord)
                    .where(
                        WorkflowStepRunRecord.organization_id == self._org,
                        WorkflowStepRunRecord.run_id == run_row.id,
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        states = {row.step_key: row.state for row in steps}
        specs = {row.step_key: await self._spec_for_step(row.version_step_id) for row in steps}
        if run_row.state != WorkflowRunState.RUNNING.value:
            return
        for row in steps:
            if row.state != WorkflowStepState.PENDING.value:
                continue
            if all(
                states[key] in {WorkflowStepState.COMPLETED.value, WorkflowStepState.SKIPPED.value}
                for key in specs[row.step_key].depends_on
            ):
                row.state = WorkflowStepState.READY.value
                row.updated_at = _utcnow()
                await self.add_transition(
                    run_row,
                    row,
                    WorkflowStepState.READY.value,
                    "DEPENDENCIES_COMPLETE",
                    "ENGINE",
                    from_state=WorkflowStepState.PENDING.value,
                )
        if all(
            row.state in {WorkflowStepState.COMPLETED.value, WorkflowStepState.SKIPPED.value}
            for row in steps
        ):
            run_row.output_payload = {
                row.step_key: row.output_payload for row in steps if row.output_payload is not None
            }
            await self._transition_run(run_row, WorkflowRunState.COMPLETED, "WORKFLOW_COMPLETED")

    async def transition_run(
        self, run_id: uuid.UUID, target: WorkflowRunState, reason: str
    ) -> WorkflowRun | None:
        row = await self.run_row(run_id, for_update=True)
        if row is None:
            return None
        await self._transition_run(row, target, reason)
        if target is WorkflowRunState.CANCELLED:
            pending = (
                (
                    await self._session.execute(
                        select(WorkflowStepRunRecord)
                        .where(
                            WorkflowStepRunRecord.organization_id == self._org,
                            WorkflowStepRunRecord.run_id == row.id,
                            WorkflowStepRunRecord.state.in_(
                                [WorkflowStepState.PENDING.value, WorkflowStepState.READY.value]
                            ),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            for step in pending:
                previous = step.state
                step.state = WorkflowStepState.CANCELLED.value
                step.updated_at = _utcnow()
                await self.add_transition(
                    row,
                    step,
                    WorkflowStepState.CANCELLED.value,
                    "WORKFLOW_CANCELLED",
                    "ENGINE",
                    from_state=previous,
                )
        elif target is WorkflowRunState.RUNNING:
            await self._advance(row)
        await self._session.flush()
        return _run(row)

    async def _transition_run(
        self, row: WorkflowRunRecord, target: WorkflowRunState, reason: str
    ) -> None:
        previous = row.state
        require_workflow_transition(WorkflowRunState(previous), target)
        row.state = target.value
        row.state_version += 1
        row.updated_at = _utcnow()
        row.error_code = reason if target is WorkflowRunState.FAILED else row.error_code
        if target in {
            WorkflowRunState.COMPLETED,
            WorkflowRunState.FAILED,
            WorkflowRunState.CANCELLED,
        }:
            row.ended_at = _utcnow()
            row.terminal_reason = reason
        await self.add_transition(row, None, target.value, reason, "ENGINE", from_state=previous)

    async def add_transition(
        self,
        run: WorkflowRunRecord,
        step: WorkflowStepRunRecord | None,
        to_state: str,
        reason: str,
        source: str,
        *,
        from_state: str | None = None,
    ) -> None:
        self._session.add(
            WorkflowTransitionHistoryRecord(
                id=uuid.uuid7(),
                organization_id=self._org,
                run_id=run.id,
                entity_type="WORKFLOW_STEP_RUN" if step else "WORKFLOW_RUN",
                entity_id=step.id if step else run.id,
                from_state=from_state,
                to_state=to_state,
                reason_code=reason[:96],
                source=source,
                correlation_id=run.correlation_id,
                detail=None,
            )
        )

    async def transitions(self, run_id: uuid.UUID, *, limit: int) -> list[WorkflowTransition]:
        rows = (
            (
                await self._session.execute(
                    select(WorkflowTransitionHistoryRecord)
                    .where(
                        WorkflowTransitionHistoryRecord.organization_id == self._org,
                        WorkflowTransitionHistoryRecord.run_id == run_id,
                    )
                    .order_by(
                        WorkflowTransitionHistoryRecord.created_at,
                        WorkflowTransitionHistoryRecord.id,
                    )
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [
            WorkflowTransition(
                id=row.id,
                organization_id=row.organization_id,
                workflow_run_id=row.run_id,
                step_run_id=row.entity_id if row.entity_type == "WORKFLOW_STEP_RUN" else None,
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                from_state=row.from_state,
                to_state=row.to_state,
                reason_code=row.reason_code,
                source=row.source,
                correlation_id=row.correlation_id,
                created_at=row.created_at,
            )
            for row in rows
        ]
