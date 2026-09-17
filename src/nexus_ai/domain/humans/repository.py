"""Tenant-scoped P17 repository and PostgreSQL fencing primitives."""

from __future__ import annotations

import datetime as dt
import hmac
import uuid
from collections.abc import Mapping

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from nexus_ai.domain.humans.models import (
    ConversationOwnershipRecord,
    HumanActionAuthorizationRecord,
    HumanAgentPresenceRecord,
    HumanAssignmentRecord,
    HumanHandoffRecord,
    HumanQueueRecord,
    HumanTransitionHistoryRecord,
    HumanWorkItemRecord,
)
from nexus_ai.humans.entities import (
    ActionAuthorization,
    AgentPresence,
    AssignmentState,
    ConversationOwnership,
    HumanAssignment,
    HumanHandoff,
    HumanQueue,
    HumanTransition,
    HumanWorkItem,
    OwnershipMode,
    PresenceState,
    WorkItemState,
)
from nexus_ai.humans.errors import (
    HumanCapacityExceededError,
    HumanConflictError,
    HumanExecutionFencedError,
    HumanInvalidStateError,
    HumanNotFoundError,
)
from nexus_ai.humans.identity import token_digest
from nexus_ai.infrastructure.tenant_session import TenantSession


def _queue(row: HumanQueueRecord) -> HumanQueue:
    return HumanQueue(
        id=row.id,
        organization_id=row.organization_id,
        queue_key=row.queue_key,
        name=row.name,
        enabled=row.enabled,
        supported_channels=tuple(row.supported_channels),
        required_skills=tuple(row.required_skills),
        max_active_assignments=row.max_active_assignments,
        sla_target_seconds=row.sla_target_seconds,
        revision=row.revision,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _presence(row: HumanAgentPresenceRecord) -> AgentPresence:
    return AgentPresence.model_validate(row, from_attributes=True)


def _work(row: HumanWorkItemRecord) -> HumanWorkItem:
    return HumanWorkItem.model_validate(row, from_attributes=True)


def _assignment(row: HumanAssignmentRecord) -> HumanAssignment:
    return HumanAssignment.model_validate(row, from_attributes=True)


def _ownership(row: ConversationOwnershipRecord) -> ConversationOwnership:
    return ConversationOwnership.model_validate(row, from_attributes=True)


def _handoff(row: HumanHandoffRecord) -> HumanHandoff:
    return HumanHandoff.model_validate(row, from_attributes=True)


def _authorization(row: HumanActionAuthorizationRecord) -> ActionAuthorization:
    return ActionAuthorization.model_validate(row, from_attributes=True)


def _transition(row: HumanTransitionHistoryRecord) -> HumanTransition:
    return HumanTransition.model_validate(row, from_attributes=True)


class HumanOperationsRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self.session = tenant.session
        self.organization_id = tenant.organization_id

    async def database_now(self) -> dt.datetime:
        value = (await self.session.execute(select(func.clock_timestamp()))).scalar_one()
        if not isinstance(value, dt.datetime):
            raise RuntimeError("database clock returned invalid time")
        return value

    async def create_queue(self, values: Mapping[str, object]) -> HumanQueue:
        row = HumanQueueRecord(id=uuid.uuid7(), organization_id=self.organization_id, **values)
        self.session.add(row)
        await self.session.flush()
        return _queue(row)

    async def queue_row(
        self, queue_id: uuid.UUID, *, lock: str | None = None
    ) -> HumanQueueRecord | None:
        query: Select[tuple[HumanQueueRecord]] = select(HumanQueueRecord).where(
            HumanQueueRecord.organization_id == self.organization_id,
            HumanQueueRecord.id == queue_id,
        )
        if lock == "update":
            query = query.with_for_update()
        elif lock == "share":
            query = query.with_for_update(read=True)
        return (await self.session.execute(query)).scalar_one_or_none()

    async def queue(self, queue_id: uuid.UUID) -> HumanQueue | None:
        row = await self.queue_row(queue_id)
        return None if row is None else _queue(row)

    async def queues(self, *, limit: int, offset: int) -> list[HumanQueue]:
        rows = (
            (
                await self.session.execute(
                    select(HumanQueueRecord)
                    .where(HumanQueueRecord.organization_id == self.organization_id)
                    .order_by(HumanQueueRecord.queue_key, HumanQueueRecord.id)
                    .offset(offset)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_queue(row) for row in rows]

    async def update_queue(
        self, row: HumanQueueRecord, expected_revision: int, values: Mapping[str, object]
    ) -> HumanQueue:
        if row.revision != expected_revision:
            raise HumanConflictError("queue revision changed")
        for key, value in values.items():
            if value is not None:
                setattr(row, key, value)
        row.revision += 1
        row.updated_at = await self.database_now()
        await self.session.flush()
        return _queue(row)

    async def presence_row(
        self, agent_user_id: uuid.UUID, *, for_update: bool = False
    ) -> HumanAgentPresenceRecord | None:
        query: Select[tuple[HumanAgentPresenceRecord]] = select(HumanAgentPresenceRecord).where(
            HumanAgentPresenceRecord.organization_id == self.organization_id,
            HumanAgentPresenceRecord.agent_user_id == agent_user_id,
        )
        if for_update:
            query = query.with_for_update()
        return (await self.session.execute(query)).scalar_one_or_none()

    async def set_presence(
        self,
        agent_user_id: uuid.UUID,
        state: PresenceState,
        capacity: int,
        expected_version: int | None,
    ) -> AgentPresence:
        row = await self.presence_row(agent_user_id, for_update=True)
        now = await self.database_now()
        if row is None:
            row = HumanAgentPresenceRecord(
                id=uuid.uuid7(),
                organization_id=self.organization_id,
                agent_user_id=agent_user_id,
                state=state.value,
                capacity=capacity,
                active_assignment_count=0,
                version=1,
                created_at=now,
                updated_at=now,
            )
            self.session.add(row)
        else:
            if expected_version is not None and row.version != expected_version:
                raise HumanConflictError("presence version changed")
            if capacity < row.active_assignment_count:
                raise HumanCapacityExceededError("capacity cannot fall below active assignments")
            row.state = state.value
            row.capacity = capacity
            row.version += 1
            row.updated_at = now
        await self.session.flush()
        return _presence(row)

    async def presence(self, agent_user_id: uuid.UUID) -> AgentPresence | None:
        row = await self.presence_row(agent_user_id)
        return None if row is None else _presence(row)

    async def list_presence(self, *, limit: int, offset: int) -> list[AgentPresence]:
        rows = (
            (
                await self.session.execute(
                    select(HumanAgentPresenceRecord)
                    .where(HumanAgentPresenceRecord.organization_id == self.organization_id)
                    .order_by(HumanAgentPresenceRecord.agent_user_id)
                    .offset(offset)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_presence(row) for row in rows]

    async def work_row(
        self, work_item_id: uuid.UUID, *, for_update: bool = False, skip_locked: bool = False
    ) -> HumanWorkItemRecord | None:
        query: Select[tuple[HumanWorkItemRecord]] = select(HumanWorkItemRecord).where(
            HumanWorkItemRecord.organization_id == self.organization_id,
            HumanWorkItemRecord.id == work_item_id,
        )
        if for_update:
            query = query.with_for_update(skip_locked=skip_locked)
        return (await self.session.execute(query)).scalar_one_or_none()

    async def work_item(self, work_item_id: uuid.UUID) -> HumanWorkItem | None:
        row = await self.work_row(work_item_id)
        return None if row is None else _work(row)

    async def create_work(self, values: Mapping[str, object]) -> HumanWorkItem:
        statement = (
            pg_insert(HumanWorkItemRecord)
            .values(id=uuid.uuid7(), organization_id=self.organization_id, **values)
            .on_conflict_do_nothing(index_elements=["organization_id", "idempotency_key"])
            .returning(HumanWorkItemRecord)
        )
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            row = (
                await self.session.execute(
                    select(HumanWorkItemRecord).where(
                        HumanWorkItemRecord.organization_id == self.organization_id,
                        HumanWorkItemRecord.idempotency_key == values["idempotency_key"],
                    )
                )
            ).scalar_one()
        return _work(row)

    async def list_work(
        self, *, queue_id: uuid.UUID | None, limit: int, offset: int
    ) -> list[HumanWorkItem]:
        query = select(HumanWorkItemRecord).where(
            HumanWorkItemRecord.organization_id == self.organization_id
        )
        if queue_id is not None:
            query = query.where(HumanWorkItemRecord.queue_id == queue_id)
        rows = (
            (
                await self.session.execute(
                    query.order_by(
                        HumanWorkItemRecord.priority.desc(),
                        HumanWorkItemRecord.eligible_at,
                        HumanWorkItemRecord.created_at,
                        HumanWorkItemRecord.id,
                    )
                    .offset(offset)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_work(row) for row in rows]

    async def ownership_row(
        self, conversation_id: uuid.UUID, *, for_update: bool = False
    ) -> ConversationOwnershipRecord | None:
        query: Select[tuple[ConversationOwnershipRecord]] = select(
            ConversationOwnershipRecord
        ).where(
            ConversationOwnershipRecord.organization_id == self.organization_id,
            ConversationOwnershipRecord.conversation_id == conversation_id,
        )
        if for_update:
            query = query.with_for_update()
        return (await self.session.execute(query)).scalar_one_or_none()

    async def ensure_ownership(
        self, conversation_id: uuid.UUID, *, initial_mode: OwnershipMode = OwnershipMode.UNASSIGNED
    ) -> ConversationOwnershipRecord:
        await self.session.execute(
            pg_insert(ConversationOwnershipRecord)
            .values(
                id=uuid.uuid7(),
                organization_id=self.organization_id,
                conversation_id=conversation_id,
                mode=initial_mode.value,
                ownership_generation=1,
                work_item_id=None,
                assignment_id=None,
                agent_user_id=None,
                ai_session_id=None,
            )
            .on_conflict_do_nothing(index_elements=["organization_id", "conversation_id"])
        )
        row = await self.ownership_row(conversation_id, for_update=True)
        if row is None:
            raise HumanExecutionFencedError("conversation ownership could not be established")
        return row

    async def ownership(self, conversation_id: uuid.UUID) -> ConversationOwnership | None:
        row = await self.ownership_row(conversation_id)
        return None if row is None else _ownership(row)

    async def claim(
        self,
        *,
        queue_id: uuid.UUID,
        expected_queue_revision: int,
        agent_user_id: uuid.UUID,
        skills: tuple[str, ...],
        claim_token: str,
    ) -> tuple[HumanWorkItem, HumanAssignment, int]:
        queue = await self.queue_row(queue_id, lock="share")
        if queue is None or not queue.enabled:
            raise HumanInvalidStateError("queue is absent or disabled")
        if queue.revision != expected_queue_revision:
            raise HumanConflictError("queue revision changed")
        if not set(queue.required_skills).issubset(skills):
            raise HumanInvalidStateError("agent does not satisfy required queue skills")
        presence = await self.presence_row(agent_user_id, for_update=True)
        if presence is None or presence.state not in {
            PresenceState.AVAILABLE.value,
            PresenceState.BUSY.value,
        }:
            raise HumanInvalidStateError("agent is not available for work")
        capacity = min(presence.capacity, queue.max_active_assignments)
        if presence.active_assignment_count >= capacity:
            raise HumanCapacityExceededError("agent has reached assignment capacity")
        now = await self.database_now()
        work = (
            await self.session.execute(
                select(HumanWorkItemRecord)
                .where(
                    HumanWorkItemRecord.organization_id == self.organization_id,
                    HumanWorkItemRecord.queue_id == queue_id,
                    HumanWorkItemRecord.state == WorkItemState.QUEUED.value,
                    HumanWorkItemRecord.eligible_at <= func.clock_timestamp(),
                )
                .order_by(
                    HumanWorkItemRecord.priority.desc(),
                    HumanWorkItemRecord.eligible_at,
                    HumanWorkItemRecord.created_at,
                    HumanWorkItemRecord.id,
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if work is None:
            raise HumanNotFoundError("no eligible work item is available")
        ownership = await self.ensure_ownership(
            work.conversation_id, initial_mode=OwnershipMode.UNASSIGNED
        )
        if ownership.mode == OwnershipMode.HUMAN.value:
            raise HumanConflictError("conversation already has human ownership")
        lease_version = work.lease_version + 1
        assignment = HumanAssignmentRecord(
            id=uuid.uuid7(),
            organization_id=self.organization_id,
            work_item_id=work.id,
            queue_id=queue.id,
            owner_agent_id=agent_user_id,
            state=AssignmentState.CLAIMED.value,
            claim_token_hash=token_digest(claim_token),
            lease_version=lease_version,
            acquired_at=now,
        )
        self.session.add(assignment)
        await self.session.flush()
        work.state = WorkItemState.CLAIMED.value
        work.assigned_agent_id = agent_user_id
        work.current_assignment_id = assignment.id
        work.lease_version = lease_version
        work.first_claim_at = work.first_claim_at or now
        work.updated_at = now
        presence.active_assignment_count += 1
        presence.state = (
            PresenceState.BUSY.value
            if presence.active_assignment_count >= capacity
            else presence.state
        )
        presence.version += 1
        presence.updated_at = now
        ownership.mode = OwnershipMode.UNASSIGNED.value
        ownership.work_item_id = work.id
        ownership.assignment_id = None
        ownership.agent_user_id = None
        ownership.ai_session_id = None
        ownership.ownership_generation += 1
        ownership.updated_at = now
        await self.session.flush()
        return _work(work), _assignment(assignment), ownership.ownership_generation

    async def assignment_row(
        self, assignment_id: uuid.UUID, *, for_update: bool = False
    ) -> HumanAssignmentRecord | None:
        query: Select[tuple[HumanAssignmentRecord]] = select(HumanAssignmentRecord).where(
            HumanAssignmentRecord.organization_id == self.organization_id,
            HumanAssignmentRecord.id == assignment_id,
        )
        if for_update:
            query = query.with_for_update()
        return (await self.session.execute(query)).scalar_one_or_none()

    async def validate_authority(
        self,
        assignment_id: uuid.UUID,
        claim_token: str,
        lease_version: int,
        ownership_generation: int,
        *,
        lock: bool = True,
        additional_queue_ids: tuple[uuid.UUID, ...] = (),
        additional_agent_ids: tuple[uuid.UUID, ...] = (),
    ) -> tuple[
        HumanAssignmentRecord,
        HumanWorkItemRecord,
        ConversationOwnershipRecord,
        HumanAgentPresenceRecord,
    ]:
        assignment, work, ownership, presence = await self.authority_context(
            assignment_id,
            lock=lock,
            additional_queue_ids=additional_queue_ids,
            additional_agent_ids=additional_agent_ids,
        )
        if assignment.state not in {
            AssignmentState.CLAIMED.value,
            AssignmentState.ACCEPTED.value,
            AssignmentState.ACTIVE.value,
        }:
            raise HumanExecutionFencedError("assignment is not active")
        self.require_claim_token(assignment, claim_token, lease_version)
        if work.current_assignment_id != assignment.id or work.lease_version != lease_version:
            raise HumanExecutionFencedError("work item no longer belongs to this assignment")
        if (
            ownership.ownership_generation != ownership_generation
            or ownership.assignment_id != assignment.id
        ):
            raise HumanExecutionFencedError("conversation ownership generation is stale")
        return assignment, work, ownership, presence

    async def authority_context(
        self,
        assignment_id: uuid.UUID,
        *,
        lock: bool = True,
        additional_queue_ids: tuple[uuid.UUID, ...] = (),
        additional_agent_ids: tuple[uuid.UUID, ...] = (),
    ) -> tuple[
        HumanAssignmentRecord,
        HumanWorkItemRecord,
        ConversationOwnershipRecord,
        HumanAgentPresenceRecord,
    ]:
        probe = await self.assignment_row(assignment_id)
        if probe is None:
            raise HumanExecutionFencedError("assignment is absent")
        work_probe = await self.work_row(probe.work_item_id)
        if work_probe is None:
            raise HumanExecutionFencedError("work item is absent")
        if lock:
            for queue_id in sorted({probe.queue_id, *additional_queue_ids}):
                if await self.queue_row(queue_id, lock="share") is None:
                    raise HumanExecutionFencedError("queue is absent")
            for agent_id in sorted({probe.owner_agent_id, *additional_agent_ids}):
                if await self.presence_row(agent_id, for_update=True) is None:
                    raise HumanExecutionFencedError("agent presence is absent")
            work = await self.work_row(probe.work_item_id, for_update=True)
            ownership = await self.ownership_row(work_probe.conversation_id, for_update=True)
            assignment = await self.assignment_row(assignment_id, for_update=True)
        else:
            assignment = probe
            work = work_probe
            ownership = await self.ownership_row(work_probe.conversation_id)
        if assignment is None or work is None or ownership is None:
            raise HumanExecutionFencedError("assignment authority context is incomplete")
        presence = await self.presence_row(assignment.owner_agent_id)
        if presence is None:
            raise HumanExecutionFencedError("agent presence is absent")
        return assignment, work, ownership, presence

    @staticmethod
    def require_claim_token(
        assignment: HumanAssignmentRecord, claim_token: str, lease_version: int
    ) -> None:
        if assignment.lease_version != lease_version or not hmac.compare_digest(
            assignment.claim_token_hash, token_digest(claim_token)
        ):
            raise HumanExecutionFencedError("claim token or lease version is stale")

    async def handoff_by_key(
        self, idempotency_key: str, *, for_update: bool = False
    ) -> HumanHandoffRecord | None:
        query = select(HumanHandoffRecord).where(
            HumanHandoffRecord.organization_id == self.organization_id,
            HumanHandoffRecord.idempotency_key == idempotency_key,
        )
        if for_update:
            query = query.with_for_update()
        return (await self.session.execute(query)).scalar_one_or_none()

    async def authorization_by_key(
        self, p09_key: str, *, for_update: bool = False
    ) -> HumanActionAuthorizationRecord | None:
        query = select(HumanActionAuthorizationRecord).where(
            HumanActionAuthorizationRecord.organization_id == self.organization_id,
            HumanActionAuthorizationRecord.p09_idempotency_key == p09_key,
        )
        if for_update:
            query = query.with_for_update()
        return (await self.session.execute(query)).scalar_one_or_none()

    async def authorization_row(
        self, authorization_id: uuid.UUID, *, for_update: bool = False
    ) -> HumanActionAuthorizationRecord | None:
        query = select(HumanActionAuthorizationRecord).where(
            HumanActionAuthorizationRecord.organization_id == self.organization_id,
            HumanActionAuthorizationRecord.id == authorization_id,
        )
        if for_update:
            query = query.with_for_update()
        return (await self.session.execute(query)).scalar_one_or_none()

    async def transition(
        self,
        *,
        actor_user_id: uuid.UUID | None,
        action: str,
        entity_type: str,
        entity_id: uuid.UUID,
        previous_state: str | None,
        new_state: str,
        reason_code: str,
        correlation_id: str | None,
        metadata: Mapping[str, object] | None = None,
    ) -> HumanTransition:
        row = HumanTransitionHistoryRecord(
            id=uuid.uuid7(),
            organization_id=self.organization_id,
            actor_user_id=actor_user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            previous_state=previous_state,
            new_state=new_state,
            reason_code=reason_code,
            correlation_id=correlation_id,
            metadata_json=dict(metadata or {}),
        )
        self.session.add(row)
        await self.session.flush()
        return _transition(row)

    async def transitions(self, entity_id: uuid.UUID, *, limit: int) -> list[HumanTransition]:
        rows = (
            (
                await self.session.execute(
                    select(HumanTransitionHistoryRecord)
                    .where(
                        HumanTransitionHistoryRecord.organization_id == self.organization_id,
                        HumanTransitionHistoryRecord.entity_id == entity_id,
                    )
                    .order_by(
                        HumanTransitionHistoryRecord.created_at, HumanTransitionHistoryRecord.id
                    )
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_transition(row) for row in rows]

    async def active_assignments(self, *, limit: int, offset: int) -> list[HumanAssignment]:
        rows = (
            (
                await self.session.execute(
                    select(HumanAssignmentRecord)
                    .where(
                        HumanAssignmentRecord.organization_id == self.organization_id,
                        HumanAssignmentRecord.state.in_(("CLAIMED", "ACCEPTED", "ACTIVE")),
                    )
                    .order_by(HumanAssignmentRecord.acquired_at, HumanAssignmentRecord.id)
                    .offset(offset)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_assignment(row) for row in rows]


__all__ = [
    "HumanOperationsRepository",
    "_assignment",
    "_authorization",
    "_handoff",
    "_ownership",
    "_presence",
    "_queue",
    "_transition",
    "_work",
]
