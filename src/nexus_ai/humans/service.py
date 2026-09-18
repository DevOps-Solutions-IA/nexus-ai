"""P17 application service: durable human authority and certified boundaries."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError

from nexus_ai.agents.entities import (
    AGENT_SESSION_CONTRACT_VERSION,
    AgentChannel,
    StartAgentSessionRequest,
    SubmitTurnRequest,
)
from nexus_ai.agents.service import AgentService
from nexus_ai.core.errors import NxsError
from nexus_ai.domain.auth.entities import Principal
from nexus_ai.domain.humans.models import (
    HumanActionAuthorizationRecord,
    HumanAssignmentRecord,
    HumanHandoffRecord,
)
from nexus_ai.domain.humans.repository import (
    HumanOperationsRepository,
    _assignment,
    _authorization,
    _ownership,
    _work,
)
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.publisher import EventPublisher
from nexus_ai.humans import events as human_events  # noqa: F401
from nexus_ai.humans.entities import (
    ActionAuthorization,
    ActionAuthorizationState,
    AgentPresence,
    AssignmentActionRequest,
    AssignmentState,
    ClaimResult,
    ClaimWorkRequest,
    ConversationOwnership,
    CopilotRequest,
    CopilotSuggestion,
    CreateQueueRequest,
    HandoffDirection,
    HandoffState,
    HumanAssignment,
    HumanQueue,
    HumanSendRequest,
    HumanTransition,
    HumanWorkItem,
    OwnershipMode,
    PresenceState,
    QueueHumanWorkRequest,
    ReturnToAiRequest,
    SetPresenceRequest,
    SupervisorActionRequest,
    SupervisorTransferToAgentRequest,
    TransferToAgentRequest,
    TransferToQueueRequest,
    UpdateQueueRequest,
    WorkItemState,
)
from nexus_ai.humans.errors import (
    HumanBoundaryUnavailableError,
    HumanCapacityExceededError,
    HumanConflictError,
    HumanExecutionFencedError,
    HumanInvalidStateError,
    HumanNotFoundError,
)
from nexus_ai.humans.identity import downstream_key, opaque_token, semantic_digest, token_digest
from nexus_ai.humans.state_machine import require_assignment_transition, require_work_transition
from nexus_ai.infrastructure.database import Database
from nexus_ai.messaging.entities import (
    MessageContent,
    OutboundAddressInput,
    SendMessageRequest,
)
from nexus_ai.messaging.service import MessagingService


class HumanOperationsService:
    def __init__(
        self,
        database: Database,
        publisher: EventPublisher,
        messaging: MessagingService,
        agents: AgentService,
        *,
        service_name: str,
    ) -> None:
        self._db = database
        self._publisher = publisher
        self._messaging = messaging
        self._agents = agents
        self._service_name = service_name

    async def create_queue(
        self, organization_id: uuid.UUID, request: CreateQueueRequest
    ) -> HumanQueue:
        try:
            async with self._db.tenant_transaction(organization_id) as tenant:
                return await HumanOperationsRepository(tenant).create_queue(
                    {
                        "queue_key": request.queue_key,
                        "name": request.name,
                        "enabled": True,
                        "supported_channels": [item.value for item in request.supported_channels],
                        "required_skills": list(request.required_skills),
                        "max_active_assignments": request.max_active_assignments,
                        "sla_target_seconds": request.sla_target_seconds,
                        "revision": 1,
                    }
                )
        except IntegrityError as exc:
            raise HumanConflictError("queue_key already exists in this Organization") from exc

    async def get_queue(self, organization_id: uuid.UUID, queue_id: uuid.UUID) -> HumanQueue:
        async with self._db.tenant_transaction(organization_id) as tenant:
            value = await HumanOperationsRepository(tenant).queue(queue_id)
            if value is None:
                raise HumanNotFoundError("queue does not exist in this Organization")
            return value

    async def list_queues(
        self, organization_id: uuid.UUID, *, limit: int, offset: int
    ) -> list[HumanQueue]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await HumanOperationsRepository(tenant).queues(limit=limit, offset=offset)

    async def update_queue(
        self, organization_id: uuid.UUID, queue_id: uuid.UUID, request: UpdateQueueRequest
    ) -> HumanQueue:
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = HumanOperationsRepository(tenant)
            row = await repo.queue_row(queue_id, lock="update")
            if row is None:
                raise HumanNotFoundError("queue does not exist in this Organization")
            values = request.model_dump(exclude={"expected_revision"}, exclude_none=True)
            if "supported_channels" in values:
                values["supported_channels"] = [
                    item.value for item in request.supported_channels or ()
                ]
            return await repo.update_queue(row, request.expected_revision, values)

    async def set_presence(
        self,
        organization_id: uuid.UUID,
        agent_user_id: uuid.UUID,
        request: SetPresenceRequest,
        *,
        actor_user_id: uuid.UUID,
        correlation_id: str | None = None,
    ) -> AgentPresence:
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = HumanOperationsRepository(tenant)
            previous = await repo.presence(agent_user_id)
            value = await repo.set_presence(
                agent_user_id, request.state, request.capacity, request.expected_version
            )
            await repo.transition(
                actor_user_id=actor_user_id,
                action="PRESENCE_CHANGED",
                entity_type="PRESENCE",
                entity_id=value.id,
                previous_state=None if previous is None else previous.state.value,
                new_state=value.state.value,
                reason_code="PRESENCE_EXPLICITLY_SET",
                correlation_id=correlation_id,
            )
            await self._event(
                tenant.session,
                organization_id,
                "human.presence.changed",
                value.id,
                value.state.value,
                actor_user_id=actor_user_id,
                correlation_id=correlation_id,
            )
            return value

    async def list_presence(
        self, organization_id: uuid.UUID, *, limit: int, offset: int
    ) -> list[AgentPresence]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await HumanOperationsRepository(tenant).list_presence(limit=limit, offset=offset)

    async def request_ai_handoff(
        self,
        organization_id: uuid.UUID,
        request: QueueHumanWorkRequest,
        *,
        actor_user_id: uuid.UUID | None = None,
    ) -> HumanWorkItem:
        async with self._db.execution_transaction(organization_id) as tenant:
            repo = HumanOperationsRepository(tenant)
            queue = await repo.queue_row(request.queue_id, lock="share")
            if (
                queue is None
                or not queue.enabled
                or request.channel.value not in queue.supported_channels
            ):
                raise HumanInvalidStateError("queue is disabled, absent, or channel-ineligible")
            existing = await repo.create_work(
                {
                    "conversation_id": request.conversation_id,
                    "customer_id": request.customer_id,
                    "channel": request.channel.value,
                    "source_type": request.source_type,
                    "source_id": request.source_id,
                    "call_id": request.call_id,
                    "voice_session_id": request.voice_session_id,
                    "queue_id": request.queue_id,
                    "priority": request.priority,
                    "state": WorkItemState.QUEUED.value,
                    "handoff_reason": request.handoff_reason,
                    "idempotency_key": request.idempotency_key,
                    "eligible_at": request.eligible_at or await repo.database_now(),
                    "assigned_agent_id": None,
                    "current_assignment_id": None,
                    "lease_version": 0,
                    "queued_at": await repo.database_now(),
                }
            )
            if (
                existing.conversation_id != request.conversation_id
                or existing.customer_id != request.customer_id
                or existing.channel != request.channel
                or existing.queue_id != request.queue_id
                or existing.priority != request.priority
                or existing.handoff_reason != request.handoff_reason
                or existing.source_type != request.source_type
                or existing.source_id != request.source_id
                or existing.call_id != request.call_id
                or existing.voice_session_id != request.voice_session_id
            ):
                raise HumanConflictError(
                    "handoff idempotency key was reused with different semantics"
                )
            ownership = await repo.ensure_ownership(request.conversation_id)
            if (
                ownership.work_item_id == existing.id
                and ownership.mode == OwnershipMode.UNASSIGNED.value
            ):
                return existing
            if ownership.mode not in {OwnershipMode.AI.value, OwnershipMode.UNASSIGNED.value}:
                raise HumanConflictError("conversation is already human-owned")
            ownership.mode = OwnershipMode.UNASSIGNED.value
            ownership.work_item_id = existing.id
            ownership.assignment_id = None
            ownership.agent_user_id = None
            ownership.ai_session_id = None
            ownership.ownership_generation += 1
            ownership.updated_at = await repo.database_now()
            handoff = HumanHandoffRecord(
                id=uuid.uuid7(),
                organization_id=organization_id,
                conversation_id=request.conversation_id,
                work_item_id=existing.id,
                direction=HandoffDirection.AI_TO_HUMAN.value,
                state=HandoffState.REQUESTED.value,
                idempotency_key=request.idempotency_key,
                semantic_fingerprint=semantic_digest(
                    {
                        "conversation_id": request.conversation_id,
                        "customer_id": request.customer_id,
                        "channel": request.channel.value,
                        "queue_id": request.queue_id,
                        "source_type": request.source_type,
                        "source_id": request.source_id,
                    }
                ),
                ownership_generation=ownership.ownership_generation,
            )
            tenant.session.add(handoff)
            await repo.transition(
                actor_user_id=actor_user_id,
                action="AI_TO_HUMAN",
                entity_type="WORK_ITEM",
                entity_id=existing.id,
                previous_state=None,
                new_state=existing.state.value,
                reason_code=request.handoff_reason,
                correlation_id=request.correlation_id,
            )
            await self._event(
                tenant.session,
                organization_id,
                "human.work.queued",
                existing.id,
                existing.state.value,
                reason_code=request.handoff_reason,
                actor_user_id=actor_user_id,
                correlation_id=request.correlation_id,
            )
            await self._event(
                tenant.session,
                organization_id,
                "human.handoff.ai_to_human",
                handoff.id,
                handoff.state,
                reason_code=request.handoff_reason,
                actor_user_id=actor_user_id,
                correlation_id=request.correlation_id,
            )
            return existing

    async def claim_work(
        self, organization_id: uuid.UUID, agent_user_id: uuid.UUID, request: ClaimWorkRequest
    ) -> ClaimResult:
        token = opaque_token()
        async with self._db.execution_transaction(organization_id) as tenant:
            repo = HumanOperationsRepository(tenant)
            work, assignment, generation = await repo.claim(
                queue_id=request.queue_id,
                expected_queue_revision=request.expected_queue_revision,
                agent_user_id=agent_user_id,
                skills=request.skills,
                claim_token=token,
            )
            await repo.transition(
                actor_user_id=agent_user_id,
                action="CLAIM",
                entity_type="WORK_ITEM",
                entity_id=work.id,
                previous_state=WorkItemState.QUEUED.value,
                new_state=work.state.value,
                reason_code="WORK_CLAIMED",
                correlation_id=request.correlation_id,
            )
            await self._event(
                tenant.session,
                organization_id,
                "human.work.claimed",
                work.id,
                work.state.value,
                actor_user_id=agent_user_id,
                correlation_id=request.correlation_id,
            )
            return ClaimResult(
                work_item=work,
                assignment=assignment,
                claim_token=token,
                ownership_generation=generation,
            )

    async def accept_assignment(
        self,
        organization_id: uuid.UUID,
        assignment_id: uuid.UUID,
        request: AssignmentActionRequest,
        *,
        actor_user_id: uuid.UUID,
    ) -> HumanWorkItem:
        async with self._db.execution_transaction(organization_id) as tenant:
            repo = HumanOperationsRepository(tenant)
            assignment, work, ownership, _ = await repo.authority_context(assignment_id)
            if assignment.owner_agent_id != actor_user_id:
                raise HumanExecutionFencedError("assignment belongs to another agent")
            repo.require_claim_token(assignment, request.claim_token, request.lease_version)
            if (
                assignment.state == AssignmentState.ACTIVE.value
                and work.state == WorkItemState.ACTIVE.value
                and work.current_assignment_id == assignment.id
                and ownership.mode == OwnershipMode.HUMAN.value
                and ownership.assignment_id == assignment.id
                and ownership.ownership_generation == request.ownership_generation + 1
            ):
                return _work(work)
            if (
                ownership.ownership_generation != request.ownership_generation
                or ownership.mode != OwnershipMode.UNASSIGNED.value
                or ownership.assignment_id is not None
                or work.current_assignment_id != assignment.id
                or work.lease_version != request.lease_version
            ):
                raise HumanExecutionFencedError("accept authority is stale")
            require_assignment_transition(
                AssignmentState(assignment.state), AssignmentState.ACCEPTED
            )
            require_assignment_transition(AssignmentState.ACCEPTED, AssignmentState.ACTIVE)
            require_work_transition(WorkItemState(work.state), WorkItemState.ACCEPTED)
            require_work_transition(WorkItemState.ACCEPTED, WorkItemState.ACTIVE)
            previous = work.state
            now = await repo.database_now()
            assignment.state = AssignmentState.ACTIVE.value
            assignment.accepted_at = now
            work.state = WorkItemState.ACTIVE.value
            work.accepted_at = now
            ownership.mode = OwnershipMode.HUMAN.value
            ownership.assignment_id = assignment.id
            ownership.agent_user_id = actor_user_id
            ownership.ownership_generation += 1
            ownership.updated_at = now
            await repo.transition(
                actor_user_id=actor_user_id,
                action="ACCEPT",
                entity_type="WORK_ITEM",
                entity_id=work.id,
                previous_state=previous,
                new_state=work.state,
                reason_code=request.reason_code,
                correlation_id=request.correlation_id,
            )
            await self._event(
                tenant.session,
                organization_id,
                "human.work.accepted",
                work.id,
                work.state,
                reason_code=request.reason_code,
                actor_user_id=actor_user_id,
                correlation_id=request.correlation_id,
            )
            await tenant.session.flush()
            return _work(work)

    async def complete_assignment(
        self,
        organization_id: uuid.UUID,
        assignment_id: uuid.UUID,
        request: AssignmentActionRequest,
        *,
        actor_user_id: uuid.UUID,
    ) -> HumanWorkItem:
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = HumanOperationsRepository(tenant)
            assignment, work, ownership, presence = await repo.authority_context(assignment_id)
            if assignment.owner_agent_id != actor_user_id:
                raise HumanExecutionFencedError("assignment belongs to another agent")
            repo.require_claim_token(assignment, request.claim_token, request.lease_version)
            if (
                assignment.state == AssignmentState.COMPLETED.value
                and assignment.release_reason == request.reason_code
                and work.state == WorkItemState.COMPLETED.value
                and ownership.mode == OwnershipMode.UNASSIGNED.value
                and ownership.ownership_generation == request.ownership_generation + 1
            ):
                return _work(work)
            if (
                assignment.state != AssignmentState.ACTIVE.value
                or work.state != WorkItemState.ACTIVE.value
                or ownership.mode != OwnershipMode.HUMAN.value
                or ownership.assignment_id != assignment.id
                or ownership.ownership_generation != request.ownership_generation
            ):
                raise HumanExecutionFencedError("completion authority is stale")
            require_work_transition(WorkItemState(work.state), WorkItemState.WRAP_UP)
            require_work_transition(WorkItemState.WRAP_UP, WorkItemState.COMPLETED)
            now = await repo.database_now()
            previous = work.state
            work.state = WorkItemState.COMPLETED.value
            work.completed_at = now
            assignment.state = AssignmentState.COMPLETED.value
            assignment.released_at = now
            assignment.release_reason = request.reason_code
            presence.active_assignment_count -= 1
            presence.version += 1
            ownership.mode = OwnershipMode.UNASSIGNED.value
            ownership.assignment_id = None
            ownership.agent_user_id = None
            ownership.ownership_generation += 1
            ownership.updated_at = now
            await repo.transition(
                actor_user_id=actor_user_id,
                action="COMPLETE",
                entity_type="WORK_ITEM",
                entity_id=work.id,
                previous_state=previous,
                new_state=work.state,
                reason_code=request.reason_code,
                correlation_id=request.correlation_id,
            )
            await self._event(
                tenant.session,
                organization_id,
                "human.work.completed",
                work.id,
                work.state,
                reason_code=request.reason_code,
                actor_user_id=actor_user_id,
                correlation_id=request.correlation_id,
            )
            await tenant.session.flush()
            return _work(work)

    async def transfer_to_queue(
        self,
        organization_id: uuid.UUID,
        assignment_id: uuid.UUID,
        request: TransferToQueueRequest,
        *,
        actor_user_id: uuid.UUID,
    ) -> HumanWorkItem:
        async with self._db.execution_transaction(organization_id) as tenant:
            repo = HumanOperationsRepository(tenant)
            assignment, work, ownership, presence = await repo.validate_authority(
                assignment_id,
                request.claim_token,
                request.lease_version,
                request.ownership_generation,
                additional_queue_ids=(request.target_queue_id,),
            )
            if assignment.owner_agent_id != actor_user_id:
                raise HumanExecutionFencedError("assignment belongs to another agent")
            target = await repo.queue_row(request.target_queue_id)
            if (
                target is None
                or not target.enabled
                or work.channel not in target.supported_channels
            ):
                raise HumanInvalidStateError("target queue is not eligible")
            previous = work.state
            now = await repo.database_now()
            assignment.state = AssignmentState.TRANSFERRED.value
            assignment.released_at = now
            assignment.release_reason = request.reason_code
            presence.active_assignment_count -= 1
            presence.version += 1
            work.queue_id = request.target_queue_id
            work.state = WorkItemState.QUEUED.value
            work.assigned_agent_id = None
            work.current_assignment_id = None
            ownership.mode = OwnershipMode.UNASSIGNED.value
            ownership.assignment_id = None
            ownership.agent_user_id = None
            ownership.ownership_generation += 1
            await repo.transition(
                actor_user_id=actor_user_id,
                action="TRANSFER_TO_QUEUE",
                entity_type="WORK_ITEM",
                entity_id=work.id,
                previous_state=previous,
                new_state=work.state,
                reason_code=request.reason_code,
                correlation_id=request.correlation_id,
            )
            await self._event(
                tenant.session,
                organization_id,
                "human.work.transferred",
                work.id,
                work.state,
                reason_code=request.reason_code,
                actor_user_id=actor_user_id,
                correlation_id=request.correlation_id,
            )
            await tenant.session.flush()
            return _work(work)

    async def transfer_to_agent(
        self,
        organization_id: uuid.UUID,
        assignment_id: uuid.UUID,
        request: TransferToAgentRequest,
        *,
        actor_user_id: uuid.UUID,
    ) -> ClaimResult:
        new_token = opaque_token()
        async with self._db.execution_transaction(organization_id) as tenant:
            repo = HumanOperationsRepository(tenant)
            assignment, work, ownership, source_presence = await repo.validate_authority(
                assignment_id,
                request.claim_token,
                request.lease_version,
                request.ownership_generation,
                additional_agent_ids=(request.target_agent_user_id,),
            )
            if assignment.owner_agent_id != actor_user_id:
                raise HumanExecutionFencedError("assignment belongs to another agent")
            target_presence = await repo.presence_row(request.target_agent_user_id)
            if target_presence is None or target_presence.state not in {"AVAILABLE", "BUSY"}:
                raise HumanInvalidStateError("target agent is unavailable")
            if target_presence.active_assignment_count >= target_presence.capacity:
                raise HumanCapacityExceededError("target agent has reached capacity")
            now = await repo.database_now()
            assignment.state = AssignmentState.TRANSFERRED.value
            assignment.released_at = now
            assignment.release_reason = request.reason_code
            source_presence.active_assignment_count -= 1
            source_presence.version += 1
            lease = work.lease_version + 1
            new_assignment = HumanAssignmentRecord(
                id=uuid.uuid7(),
                organization_id=organization_id,
                work_item_id=work.id,
                queue_id=work.queue_id,
                owner_agent_id=request.target_agent_user_id,
                state=AssignmentState.ACTIVE.value,
                claim_token_hash=token_digest(new_token),
                lease_version=lease,
                acquired_at=now,
                accepted_at=now,
            )
            tenant.session.add(new_assignment)
            await tenant.session.flush()
            target_presence.active_assignment_count += 1
            target_presence.version += 1
            work.state = WorkItemState.ACTIVE.value
            work.assigned_agent_id = request.target_agent_user_id
            work.current_assignment_id = new_assignment.id
            work.lease_version = lease
            ownership.mode = OwnershipMode.HUMAN.value
            ownership.assignment_id = new_assignment.id
            ownership.agent_user_id = request.target_agent_user_id
            ownership.ownership_generation += 1
            await repo.transition(
                actor_user_id=actor_user_id,
                action="TRANSFER_TO_AGENT",
                entity_type="WORK_ITEM",
                entity_id=work.id,
                previous_state=WorkItemState.ACTIVE.value,
                new_state=work.state,
                reason_code=request.reason_code,
                correlation_id=request.correlation_id,
            )
            await self._event(
                tenant.session,
                organization_id,
                "human.work.transferred",
                work.id,
                work.state,
                reason_code=request.reason_code,
                actor_user_id=actor_user_id,
                correlation_id=request.correlation_id,
            )
            await tenant.session.flush()
            return ClaimResult(
                work_item=_work(work),
                assignment=_assignment(new_assignment),
                claim_token=new_token,
                ownership_generation=ownership.ownership_generation,
            )

    async def supervisor_release(
        self,
        organization_id: uuid.UUID,
        assignment_id: uuid.UUID,
        request: SupervisorActionRequest,
        *,
        actor_user_id: uuid.UUID,
    ) -> HumanWorkItem:
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = HumanOperationsRepository(tenant)
            probe = await repo.assignment_row(assignment_id)
            if probe is None:
                raise HumanNotFoundError("assignment does not exist")
            work_probe = await repo.work_row(probe.work_item_id)
            if work_probe is None:
                raise HumanNotFoundError("work item does not exist")
            target_queue = None
            for queue_id in sorted(
                {
                    probe.queue_id,
                    *(() if request.target_queue_id is None else (request.target_queue_id,)),
                }
            ):
                row = await repo.queue_row(queue_id, lock="share")
                if row is None:
                    raise HumanInvalidStateError("target queue is unavailable")
                if queue_id == request.target_queue_id:
                    target_queue = row
            presence = await repo.presence_row(probe.owner_agent_id, for_update=True)
            work = await repo.work_row(probe.work_item_id, for_update=True)
            ownership = await repo.ownership_row(work_probe.conversation_id, for_update=True)
            assignment = await repo.assignment_row(assignment_id, for_update=True)
            if (
                assignment is None
                or work is None
                or ownership is None
                or presence is None
                or assignment.state not in {"CLAIMED", "ACCEPTED", "ACTIVE"}
            ):
                raise HumanExecutionFencedError("assignment is no longer releasable")
            now = await repo.database_now()
            previous = work.state
            assignment.state = AssignmentState.RELEASED.value
            assignment.released_at = now
            assignment.release_reason = request.reason_code
            presence.active_assignment_count -= 1
            presence.version += 1
            work.state = WorkItemState.QUEUED.value
            work.current_assignment_id = None
            work.assigned_agent_id = None
            if request.target_queue_id is not None:
                if target_queue is None or not target_queue.enabled:
                    raise HumanInvalidStateError("target queue is unavailable")
                if work.channel not in target_queue.supported_channels:
                    raise HumanInvalidStateError("target queue does not support this channel")
                work.queue_id = target_queue.id
            ownership.mode = OwnershipMode.UNASSIGNED.value
            ownership.assignment_id = None
            ownership.agent_user_id = None
            ownership.ownership_generation += 1
            await repo.transition(
                actor_user_id=actor_user_id,
                action="SUPERVISOR_RELEASE",
                entity_type="WORK_ITEM",
                entity_id=work.id,
                previous_state=previous,
                new_state=work.state,
                reason_code=request.reason_code,
                correlation_id=request.correlation_id,
            )
            await self._event(
                tenant.session,
                organization_id,
                "human.supervisor.released",
                work.id,
                work.state,
                reason_code=request.reason_code,
                actor_user_id=actor_user_id,
                correlation_id=request.correlation_id,
            )
            await tenant.session.flush()
            return _work(work)

    async def supervisor_transfer_to_agent(
        self,
        organization_id: uuid.UUID,
        assignment_id: uuid.UUID,
        request: SupervisorTransferToAgentRequest,
        *,
        actor_user_id: uuid.UUID,
    ) -> ClaimResult:
        new_token = opaque_token()
        async with self._db.execution_transaction(organization_id) as tenant:
            repo = HumanOperationsRepository(tenant)
            probe = await repo.assignment_row(assignment_id)
            if probe is None:
                raise HumanNotFoundError("assignment does not exist")
            work_probe = await repo.work_row(probe.work_item_id)
            if work_probe is None:
                raise HumanNotFoundError("work item does not exist")
            await repo.queue_row(probe.queue_id, lock="share")
            presence_rows = {}
            for agent_id in sorted({probe.owner_agent_id, request.target_agent_user_id}):
                row = await repo.presence_row(agent_id, for_update=True)
                if row is None:
                    raise HumanInvalidStateError("source or target presence is absent")
                presence_rows[agent_id] = row
            source_presence = presence_rows[probe.owner_agent_id]
            target_presence = presence_rows[request.target_agent_user_id]
            work = await repo.work_row(probe.work_item_id, for_update=True)
            ownership = await repo.ownership_row(work_probe.conversation_id, for_update=True)
            assignment = await repo.assignment_row(assignment_id, for_update=True)
            if (
                assignment is None
                or work is None
                or ownership is None
                or assignment.state
                not in {
                    AssignmentState.CLAIMED.value,
                    AssignmentState.ACCEPTED.value,
                    AssignmentState.ACTIVE.value,
                }
                or work.current_assignment_id != assignment.id
            ):
                raise HumanExecutionFencedError("assignment is no longer transferable")
            claimed_without_human_owner = (
                assignment.state == AssignmentState.CLAIMED.value
                and ownership.mode == OwnershipMode.UNASSIGNED.value
                and ownership.assignment_id is None
                and ownership.agent_user_id is None
            )
            active_human_owner = (
                assignment.state in {AssignmentState.ACCEPTED.value, AssignmentState.ACTIVE.value}
                and ownership.mode == OwnershipMode.HUMAN.value
                and ownership.assignment_id == assignment.id
                and ownership.agent_user_id == assignment.owner_agent_id
            )
            if not claimed_without_human_owner and not active_human_owner:
                raise HumanExecutionFencedError("assignment ownership is no longer transferable")
            if assignment.owner_agent_id == request.target_agent_user_id:
                raise HumanInvalidStateError("target agent already owns this assignment")
            if target_presence.state not in {
                PresenceState.AVAILABLE.value,
                PresenceState.BUSY.value,
            }:
                raise HumanInvalidStateError("target agent is unavailable")
            if target_presence.active_assignment_count >= target_presence.capacity:
                raise HumanCapacityExceededError("target agent has reached capacity")
            now = await repo.database_now()
            assignment.state = AssignmentState.TRANSFERRED.value
            assignment.released_at = now
            assignment.release_reason = request.reason_code
            source_presence.active_assignment_count -= 1
            source_presence.version += 1
            lease = work.lease_version + 1
            new_assignment = HumanAssignmentRecord(
                id=uuid.uuid7(),
                organization_id=organization_id,
                work_item_id=work.id,
                queue_id=work.queue_id,
                owner_agent_id=request.target_agent_user_id,
                state=AssignmentState.ACTIVE.value,
                claim_token_hash=token_digest(new_token),
                lease_version=lease,
                acquired_at=now,
                accepted_at=now,
            )
            tenant.session.add(new_assignment)
            await tenant.session.flush()
            target_presence.active_assignment_count += 1
            target_presence.version += 1
            work.state = WorkItemState.ACTIVE.value
            work.assigned_agent_id = request.target_agent_user_id
            work.current_assignment_id = new_assignment.id
            work.lease_version = lease
            ownership.mode = OwnershipMode.HUMAN.value
            ownership.assignment_id = new_assignment.id
            ownership.agent_user_id = request.target_agent_user_id
            ownership.ownership_generation += 1
            ownership.updated_at = now
            await repo.transition(
                actor_user_id=actor_user_id,
                action="SUPERVISOR_TRANSFER_TO_AGENT",
                entity_type="WORK_ITEM",
                entity_id=work.id,
                previous_state=work.state,
                new_state=work.state,
                reason_code=request.reason_code,
                correlation_id=request.correlation_id,
            )
            await self._event(
                tenant.session,
                organization_id,
                "human.work.transferred",
                work.id,
                work.state,
                reason_code=request.reason_code,
                actor_user_id=actor_user_id,
                correlation_id=request.correlation_id,
            )
            await tenant.session.flush()
            return ClaimResult(
                work_item=_work(work),
                assignment=_assignment(new_assignment),
                claim_token=new_token,
                ownership_generation=ownership.ownership_generation,
            )

    async def cancel_work(
        self,
        organization_id: uuid.UUID,
        work_item_id: uuid.UUID,
        request: SupervisorActionRequest,
        *,
        actor_user_id: uuid.UUID,
    ) -> HumanWorkItem:
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = HumanOperationsRepository(tenant)
            probe = await repo.work_row(work_item_id)
            if probe is None:
                raise HumanNotFoundError("work item does not exist")
            await repo.queue_row(probe.queue_id, lock="share")
            presence = None
            if probe.assigned_agent_id is not None:
                presence = await repo.presence_row(probe.assigned_agent_id, for_update=True)
            work = await repo.work_row(work_item_id, for_update=True)
            ownership = await repo.ownership_row(probe.conversation_id, for_update=True)
            assignment = None
            if probe.current_assignment_id is not None:
                assignment = await repo.assignment_row(probe.current_assignment_id, for_update=True)
            if work is None:
                raise HumanNotFoundError("work item does not exist")
            if work.state in {WorkItemState.COMPLETED.value, WorkItemState.CANCELLED.value}:
                if work.state == WorkItemState.CANCELLED.value:
                    return _work(work)
                raise HumanInvalidStateError("completed work is terminal")
            previous = work.state
            now = await repo.database_now()
            work.state = WorkItemState.CANCELLED.value
            work.completed_at = now
            work.current_assignment_id = None
            work.assigned_agent_id = None
            if assignment is not None and assignment.state in {
                AssignmentState.CLAIMED.value,
                AssignmentState.ACCEPTED.value,
                AssignmentState.ACTIVE.value,
            }:
                assignment.state = AssignmentState.RELEASED.value
                assignment.released_at = now
                assignment.release_reason = request.reason_code
                if presence is not None:
                    presence.active_assignment_count -= 1
                    presence.version += 1
            if ownership is not None:
                ownership.mode = OwnershipMode.UNASSIGNED.value
                ownership.assignment_id = None
                ownership.agent_user_id = None
                ownership.ownership_generation += 1
                ownership.updated_at = now
            await repo.transition(
                actor_user_id=actor_user_id,
                action="CANCEL",
                entity_type="WORK_ITEM",
                entity_id=work.id,
                previous_state=previous,
                new_state=work.state,
                reason_code=request.reason_code,
                correlation_id=request.correlation_id,
            )
            await self._event(
                tenant.session,
                organization_id,
                "human.work.cancelled",
                work.id,
                work.state,
                reason_code=request.reason_code,
                actor_user_id=actor_user_id,
                correlation_id=request.correlation_id,
            )
            await tenant.session.flush()
            return _work(work)

    async def validate_ai_output_authority(
        self,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        *,
        ownership_generation: int,
        ai_session_id: uuid.UUID,
    ) -> None:
        async with self._db.execution_transaction(organization_id) as tenant:
            ownership = await HumanOperationsRepository(tenant).ownership_row(conversation_id)
            if (
                ownership is None
                or ownership.mode != OwnershipMode.AI.value
                or ownership.ownership_generation != ownership_generation
                or ownership.ai_session_id != ai_session_id
            ):
                raise HumanExecutionFencedError("AI output authority is stale")

    async def return_to_ai(
        self,
        organization_id: uuid.UUID,
        assignment_id: uuid.UUID,
        request: ReturnToAiRequest,
        *,
        principal: Principal,
    ) -> ConversationOwnership:
        p13_key = downstream_key("p13-return", request.idempotency_key)
        return_fingerprint = semantic_digest(
            {
                "assignment_id": assignment_id,
                "ownership_generation": request.ownership_generation,
                "agent_id": request.agent_id,
                "context": request.context,
            }
        )
        handoff_id: uuid.UUID
        conversation_id: uuid.UUID
        customer_id: uuid.UUID | None
        async with self._db.execution_transaction(organization_id) as tenant:
            repo = HumanOperationsRepository(tenant)
            replay = await repo.handoff_by_key(request.idempotency_key)
            if replay is not None:
                if (
                    replay.p13_idempotency_key != p13_key
                    or replay.p13_contract_version != AGENT_SESSION_CONTRACT_VERSION
                    or replay.semantic_fingerprint != return_fingerprint
                ):
                    raise HumanConflictError("return-to-AI idempotency fingerprint changed")
                if replay.state == HandoffState.ACCEPTED.value:
                    ownership = await repo.ownership_row(replay.conversation_id)
                    if (
                        ownership is None
                        or ownership.mode != OwnershipMode.AI.value
                        or ownership.ai_session_id != replay.p13_session_id
                        or ownership.ownership_generation != replay.ownership_generation + 1
                    ):
                        raise HumanExecutionFencedError(
                            "accepted AI ownership is no longer current"
                        )
                    return _ownership(ownership)
                if replay.state == HandoffState.P13_REJECTED.value:
                    raise HumanInvalidStateError("the prior P13 return request was rejected")
                if replay.state == HandoffState.AMBIGUOUS.value:
                    raise HumanBoundaryUnavailableError(
                        "P13 acceptance is ambiguous; reconciliation is deferred to NXS-P25"
                    )
                handoff = replay
                work = await repo.work_row(replay.work_item_id)
                if work is None:
                    raise HumanExecutionFencedError("handoff work item is absent")
            else:
                assignment, work, ownership, presence = await repo.validate_authority(
                    assignment_id,
                    request.claim_token,
                    request.lease_version,
                    request.ownership_generation,
                )
                if (
                    assignment.owner_agent_id != principal.user_id
                    or ownership.mode != OwnershipMode.HUMAN.value
                ):
                    raise HumanExecutionFencedError("human ownership is not current")
                now = await repo.database_now()
                assignment.state = AssignmentState.RELEASED.value
                assignment.released_at = now
                assignment.release_reason = "AI_RETURN_REQUESTED"
                presence.active_assignment_count -= 1
                presence.version += 1
                work.state = WorkItemState.AI_RETURN_PENDING.value
                ownership.mode = OwnershipMode.UNASSIGNED.value
                ownership.agent_user_id = None
                ownership.assignment_id = None
                ownership.ai_session_id = None
                ownership.ownership_generation += 1
                ownership.updated_at = now
                handoff = HumanHandoffRecord(
                    id=uuid.uuid7(),
                    organization_id=organization_id,
                    conversation_id=work.conversation_id,
                    work_item_id=work.id,
                    direction=HandoffDirection.HUMAN_TO_AI.value,
                    state=HandoffState.AI_RETURN_PENDING.value,
                    idempotency_key=request.idempotency_key,
                    semantic_fingerprint=return_fingerprint,
                    ownership_generation=ownership.ownership_generation,
                    p13_idempotency_key=p13_key,
                    p13_contract_version=AGENT_SESSION_CONTRACT_VERSION,
                )
                tenant.session.add(handoff)
                await repo.transition(
                    actor_user_id=principal.user_id,
                    action="AI_RETURN_REQUESTED",
                    entity_type="HANDOFF",
                    entity_id=handoff.id,
                    previous_state=None,
                    new_state=handoff.state,
                    reason_code=request.reason_code,
                    correlation_id=request.correlation_id,
                )
                await self._event(
                    tenant.session,
                    organization_id,
                    "human.handoff.human_to_ai",
                    handoff.id,
                    handoff.state,
                    reason_code="AI_RETURN_PENDING",
                    actor_user_id=principal.user_id,
                    correlation_id=request.correlation_id,
                )
                await tenant.session.flush()
            handoff_id = handoff.id
            conversation_id = handoff.conversation_id
            customer_id = work.customer_id

        try:
            session = await self._agents.start_session(
                organization_id,
                principal,
                StartAgentSessionRequest(
                    agent_id=request.agent_id,
                    channel=AgentChannel(work.channel),
                    conversation_id=conversation_id,
                    customer_id=customer_id,
                    correlation_id=request.correlation_id,
                    idempotency_key=p13_key,
                    metadata={"purpose": "human_return"},
                ),
            )
        except NxsError as exc:
            await self._reject_ai_return(
                organization_id, handoff_id, exc.code, principal.user_id, request.correlation_id
            )
            raise HumanInvalidStateError("P13 rejected the return-to-AI request") from exc
        except Exception as exc:
            await self._mark_ai_return_ambiguous(organization_id, handoff_id, type(exc).__name__)
            raise HumanBoundaryUnavailableError(
                "P13 acceptance is ambiguous; reconciliation is deferred to NXS-P25"
            ) from exc

        if (
            session.organization_id != organization_id
            or session.conversation_id != conversation_id
            or session.agent_id != request.agent_id
            or session.idempotency_key != p13_key
            or session.CONTRACT_VERSION != AGENT_SESSION_CONTRACT_VERSION
        ):
            await self._mark_ai_return_ambiguous(
                organization_id,
                handoff_id,
                (
                    "P13_CONTRACT_VERSION_MISMATCH"
                    if session.CONTRACT_VERSION != AGENT_SESSION_CONTRACT_VERSION
                    else "P13_EXECUTION_IDENTITY_MISMATCH"
                ),
            )
            if session.CONTRACT_VERSION != AGENT_SESSION_CONTRACT_VERSION:
                raise HumanExecutionFencedError("P13 returned a mismatched contract version")
            raise HumanExecutionFencedError("P13 returned a mismatched execution identity")

        async with self._db.execution_transaction(organization_id) as tenant:
            repo = HumanOperationsRepository(tenant)
            handoff_probe = await repo.handoff_by_key(request.idempotency_key)
            if handoff_probe is None or handoff_probe.id != handoff_id:
                raise HumanExecutionFencedError("return-to-AI handoff is absent")
            work_probe = await repo.work_row(handoff_probe.work_item_id)
            if work_probe is None:
                raise HumanExecutionFencedError("return-to-AI work item is absent")
            await repo.queue_row(work_probe.queue_id, lock="share")
            work_row = await repo.work_row(work_probe.id, for_update=True)
            ownership = await repo.ownership_row(conversation_id, for_update=True)
            bound_handoff = await repo.handoff_by_key(request.idempotency_key, for_update=True)
            if bound_handoff is None or bound_handoff.id != handoff_id:
                raise HumanExecutionFencedError("return-to-AI handoff changed")
            if (
                bound_handoff.organization_id != organization_id
                or bound_handoff.conversation_id != conversation_id
                or bound_handoff.p13_idempotency_key != p13_key
                or bound_handoff.p13_contract_version != AGENT_SESSION_CONTRACT_VERSION
                or bound_handoff.p13_contract_version != session.CONTRACT_VERSION
            ):
                raise HumanExecutionFencedError("return-to-AI execution contract changed")
            if (
                bound_handoff.p13_session_id is not None
                and bound_handoff.p13_session_id != session.id
            ):
                raise HumanConflictError("a different P13 execution is already bound")
            if (
                bound_handoff.state == HandoffState.ACCEPTED.value
                and bound_handoff.p13_session_id == session.id
                and ownership is not None
                and ownership.mode == OwnershipMode.AI.value
                and ownership.ai_session_id == session.id
                and ownership.ownership_generation == bound_handoff.ownership_generation + 1
            ):
                return _ownership(ownership)
            if (
                ownership is None
                or work_row is None
                or ownership.mode != OwnershipMode.UNASSIGNED.value
                or ownership.ownership_generation != bound_handoff.ownership_generation
            ):
                raise HumanExecutionFencedError("AI return ownership fence changed")
            bound_handoff.p13_session_id = session.id
            bound_handoff.state = HandoffState.ACCEPTED.value
            ownership.mode = OwnershipMode.AI.value
            ownership.ai_session_id = session.id
            ownership.ownership_generation += 1
            ownership.updated_at = await repo.database_now()
            work_row.state = WorkItemState.COMPLETED.value
            work_row.completed_at = await repo.database_now()
            await repo.transition(
                actor_user_id=principal.user_id,
                action="AI_RETURN_ACCEPTED",
                entity_type="HANDOFF",
                entity_id=bound_handoff.id,
                previous_state=HandoffState.AI_RETURN_PENDING.value,
                new_state=bound_handoff.state,
                reason_code="P13_BOUND",
                correlation_id=request.correlation_id,
                metadata={"p13_session_id": str(session.id)},
            )
            await tenant.session.flush()
            return _ownership(ownership)

    async def _reject_ai_return(
        self,
        organization_id: uuid.UUID,
        handoff_id: uuid.UUID,
        code: str,
        actor_user_id: uuid.UUID,
        correlation_id: str | None,
    ) -> None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = HumanOperationsRepository(tenant)
            handoff_probe = await tenant.session.get(HumanHandoffRecord, handoff_id)
            if (
                handoff_probe is None
                or handoff_probe.organization_id != organization_id
                or handoff_probe.state != HandoffState.AI_RETURN_PENDING.value
            ):
                return
            work_probe = await repo.work_row(handoff_probe.work_item_id)
            if work_probe is None:
                return
            await repo.queue_row(work_probe.queue_id, lock="share")
            work = await repo.work_row(work_probe.id, for_update=True)
            ownership = await repo.ownership_row(handoff_probe.conversation_id, for_update=True)
            handoff = await tenant.session.get(HumanHandoffRecord, handoff_id, with_for_update=True)
            if (
                work is None
                or ownership is None
                or handoff is None
                or handoff.state != HandoffState.AI_RETURN_PENDING.value
            ):
                return
            handoff.state = HandoffState.P13_REJECTED.value
            handoff.error_code = code
            work.state = WorkItemState.QUEUED.value
            work.current_assignment_id = None
            work.assigned_agent_id = None
            ownership.mode = OwnershipMode.UNASSIGNED.value
            ownership.assignment_id = None
            ownership.agent_user_id = None
            ownership.ai_session_id = None
            ownership.ownership_generation += 1
            await repo.transition(
                actor_user_id=actor_user_id,
                action="AI_RETURN_REJECTED",
                entity_type="HANDOFF",
                entity_id=handoff.id,
                previous_state=HandoffState.AI_RETURN_PENDING.value,
                new_state=handoff.state,
                reason_code=code,
                correlation_id=correlation_id,
            )
            await self._event(
                tenant.session,
                organization_id,
                "human.work.requeued",
                work.id,
                work.state,
                reason_code="AI_RETURN_REJECTED",
                actor_user_id=actor_user_id,
                correlation_id=correlation_id,
            )

    async def _mark_ai_return_ambiguous(
        self, organization_id: uuid.UUID, handoff_id: uuid.UUID, code: str
    ) -> None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            row = await tenant.session.get(HumanHandoffRecord, handoff_id, with_for_update=True)
            if (
                row is not None
                and row.organization_id == organization_id
                and row.state == HandoffState.AI_RETURN_PENDING.value
            ):
                row.state = HandoffState.AMBIGUOUS.value
                row.error_code = code[:96]

    async def send_message(
        self,
        organization_id: uuid.UUID,
        assignment_id: uuid.UUID,
        request: HumanSendRequest,
        *,
        actor_user_id: uuid.UUID,
    ) -> ActionAuthorization:
        fingerprint = semantic_digest(
            {
                "assignment_id": assignment_id,
                "conversation_generation": request.ownership_generation,
                "channel": request.channel.value,
                "account_id": request.account_id,
                "to": request.to,
                "content": request.content,
            }
        )
        p09_key = downstream_key("p09-send", request.idempotency_key)
        async with self._db.execution_transaction(organization_id) as tenant:
            repo = HumanOperationsRepository(tenant)
            replay = await repo.authorization_by_key(p09_key, for_update=True)
            if replay is not None:
                if replay.semantic_fingerprint != fingerprint:
                    raise HumanConflictError(
                        "message idempotency key was reused with different semantics"
                    )
                if replay.state == ActionAuthorizationState.CONSUMED.value:
                    return _authorization(replay)
                if replay.state == ActionAuthorizationState.AMBIGUOUS.value:
                    raise HumanBoundaryUnavailableError(
                        "P09 delivery is ambiguous; reconciliation is deferred to NXS-P25"
                    )
                if replay.state == ActionAuthorizationState.FAILED.value:
                    raise HumanInvalidStateError("the prior P09 delivery attempt failed")
                authorization = replay
                work = await repo.work_row(replay.work_item_id)
                if work is None:
                    raise HumanExecutionFencedError("authorized work item is absent")
            else:
                assignment, work, ownership, _ = await repo.validate_authority(
                    assignment_id,
                    request.claim_token,
                    request.lease_version,
                    request.ownership_generation,
                )
                if (
                    assignment.owner_agent_id != actor_user_id
                    or ownership.mode != OwnershipMode.HUMAN.value
                ):
                    raise HumanExecutionFencedError("human messaging authority is not current")
                if work.channel != request.channel.value:
                    raise HumanInvalidStateError("message channel does not match the work item")
                authorization = HumanActionAuthorizationRecord(
                    id=uuid.uuid7(),
                    organization_id=organization_id,
                    conversation_id=work.conversation_id,
                    work_item_id=work.id,
                    assignment_id=assignment.id,
                    lease_version=assignment.lease_version,
                    ownership_generation=ownership.ownership_generation,
                    channel=request.channel.value,
                    semantic_fingerprint=fingerprint,
                    p09_idempotency_key=p09_key,
                    state=ActionAuthorizationState.AUTHORIZED.value,
                )
                tenant.session.add(authorization)
                await tenant.session.flush()

        try:
            message = await self._messaging.send(
                organization_id,
                actor_user_id,
                SendMessageRequest(
                    account_id=request.account_id,
                    conversation_id=authorization.conversation_id,
                    to=tuple(OutboundAddressInput(value=value) for value in request.to),
                    content=MessageContent(text=request.content),
                    idempotency_key=p09_key,
                    correlation_id=request.correlation_id,
                ),
            )
        except NxsError as exc:
            await self._finalize_authorization(
                organization_id,
                authorization.id,
                ActionAuthorizationState.FAILED,
                error_code=exc.code,
            )
            raise
        except Exception as exc:
            await self._finalize_authorization(
                organization_id,
                authorization.id,
                ActionAuthorizationState.AMBIGUOUS,
                error_code=type(exc).__name__,
            )
            raise HumanBoundaryUnavailableError(
                "P09 delivery is ambiguous; reconciliation is deferred to NXS-P25"
            ) from exc
        return await self._finalize_authorization(
            organization_id,
            authorization.id,
            ActionAuthorizationState.CONSUMED,
            message_id=message.id,
        )

    async def _finalize_authorization(
        self,
        organization_id: uuid.UUID,
        authorization_id: uuid.UUID,
        state: ActionAuthorizationState,
        *,
        message_id: uuid.UUID | None = None,
        error_code: str | None = None,
    ) -> ActionAuthorization:
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = HumanOperationsRepository(tenant)
            probe = await repo.authorization_row(authorization_id)
            if probe is None:
                raise HumanExecutionFencedError("action authorization is absent")
            work_probe = await repo.work_row(probe.work_item_id)
            if work_probe is None:
                raise HumanExecutionFencedError("authorization work item is absent")
            if await repo.queue_row(work_probe.queue_id, lock="share") is None:
                raise HumanExecutionFencedError("authorization queue is absent")
            work = await repo.work_row(work_probe.id, for_update=True)
            row = await repo.authorization_row(authorization_id, for_update=True)
            if row is None or work is None:
                raise HumanExecutionFencedError("action authorization changed")
            await tenant.session.refresh(work)
            await tenant.session.refresh(row)
            if row.state == ActionAuthorizationState.CONSUMED.value:
                return _authorization(row)
            if row.state != ActionAuthorizationState.AUTHORIZED.value:
                raise HumanExecutionFencedError("action authorization is no longer active")
            row.state = state.value
            row.message_id = message_id
            row.error_code = error_code
            if state is ActionAuthorizationState.CONSUMED and work.first_response_at is None:
                work.first_response_at = await repo.database_now()
            await tenant.session.flush()
            return _authorization(row)

    async def copilot(
        self,
        organization_id: uuid.UUID,
        assignment_id: uuid.UUID,
        request: CopilotRequest,
        *,
        principal: Principal,
    ) -> CopilotSuggestion:
        async with self._db.execution_transaction(organization_id) as tenant:
            repo = HumanOperationsRepository(tenant)
            assignment, work, ownership, _ = await repo.validate_authority(
                assignment_id,
                request.claim_token,
                request.lease_version,
                request.ownership_generation,
            )
            if (
                assignment.owner_agent_id != principal.user_id
                or ownership.mode != OwnershipMode.HUMAN.value
            ):
                raise HumanExecutionFencedError("copilot request lacks current human authority")
            conversation_id, customer_id, channel = (
                work.conversation_id,
                work.customer_id,
                work.channel,
            )
        session = await self._agents.start_session(
            organization_id,
            principal,
            StartAgentSessionRequest(
                agent_id=request.agent_id,
                channel=AgentChannel(channel),
                conversation_id=conversation_id,
                customer_id=customer_id,
                correlation_id=request.correlation_id,
                idempotency_key=downstream_key("p13-copilot-session", request.idempotency_key),
                metadata={"purpose": "human_copilot"},
            ),
        )
        response = await self._agents.submit_turn(
            organization_id,
            session.id,
            SubmitTurnRequest(
                content=request.prompt,
                idempotency_key=downstream_key("p13-copilot-turn", request.idempotency_key),
                correlation_id=request.correlation_id,
            ),
        )
        return CopilotSuggestion(session_id=session.id, suggestion=response.content)

    async def get_work_item(
        self, organization_id: uuid.UUID, work_item_id: uuid.UUID
    ) -> HumanWorkItem:
        async with self._db.tenant_transaction(organization_id) as tenant:
            value = await HumanOperationsRepository(tenant).work_item(work_item_id)
            if value is None:
                raise HumanNotFoundError("work item does not exist in this Organization")
            return value

    async def list_work(
        self, organization_id: uuid.UUID, *, queue_id: uuid.UUID | None, limit: int, offset: int
    ) -> list[HumanWorkItem]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await HumanOperationsRepository(tenant).list_work(
                queue_id=queue_id, limit=limit, offset=offset
            )

    async def get_ownership(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> ConversationOwnership:
        async with self._db.tenant_transaction(organization_id) as tenant:
            value = await HumanOperationsRepository(tenant).ownership(conversation_id)
            if value is None:
                raise HumanNotFoundError("conversation ownership has not been established")
            return value

    async def list_assignments(
        self, organization_id: uuid.UUID, *, limit: int, offset: int
    ) -> list[HumanAssignment]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await HumanOperationsRepository(tenant).active_assignments(
                limit=limit, offset=offset
            )

    async def transitions(
        self, organization_id: uuid.UUID, entity_id: uuid.UUID, *, limit: int
    ) -> list[HumanTransition]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await HumanOperationsRepository(tenant).transitions(entity_id, limit=limit)

    async def _event(
        self,
        session: Any,
        organization_id: uuid.UUID,
        event_type: str,
        entity_id: uuid.UUID,
        state: str,
        *,
        reason_code: str | None = None,
        actor_user_id: uuid.UUID | None = None,
        correlation_id: str | None = None,
    ) -> None:
        await self._publisher.enqueue(
            session,
            EventEnvelope.create(
                event_type=event_type,
                event_version=1,
                aggregate_type="human_operations",
                aggregate_id=str(entity_id),
                producer=self._service_name,
                organization_id=organization_id,
                correlation_id=correlation_id,
                payload={
                    "entity_id": str(entity_id),
                    "state": state,
                    "reason_code": reason_code,
                    "actor_user_id": None if actor_user_id is None else str(actor_user_id),
                },
            ),
        )
