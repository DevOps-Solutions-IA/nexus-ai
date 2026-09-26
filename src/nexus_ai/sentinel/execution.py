"""DB-time ownership and dispatch CAS; ambiguous effects never redispatch."""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid7

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai.domain.sentinel.models import (
    SentinelActionProposal,
    SentinelApproval,
    SentinelExecution,
)
from nexus_ai.sentinel.contracts import Approval, Proposal, approval_current, proposal_fingerprint
from nexus_ai.sentinel.errors import SentinelConflict, SentinelDenied
from nexus_ai.sentinel.policy import risk_allowed
from nexus_ai.sentinel.runbooks import (
    ActionResult,
    DispatchRequest,
    ProvenPreEffectRejection,
    SentinelActionAdapter,
    SentinelRunbookRegistry,
)
from nexus_ai.sentinel.service import SentinelStore


@dataclass(frozen=True)
class ExecutionClaim:
    proposal_id: UUID
    execution_id: UUID
    owner_id: UUID
    generation: int
    idempotency_key: str


class SentinelActionExecutor:
    def __init__(self, store: SentinelStore, registry: SentinelRunbookRegistry) -> None:
        self.store = store
        self.registry = registry

    async def _lock_proposal(self, session: AsyncSession, identity: UUID) -> SentinelActionProposal:
        incident_id = await session.scalar(
            select(SentinelActionProposal.incident_id).where(SentinelActionProposal.id == identity)
        )
        if incident_id is None:
            raise SentinelDenied("proposal_not_found")
        await self.store._incident(session, incident_id)
        row = await session.scalar(
            select(SentinelActionProposal)
            .where(SentinelActionProposal.id == identity)
            .with_for_update()
        )
        if row is None:
            raise SentinelDenied("proposal_not_found")
        return row

    async def _authorize(
        self, session: AsyncSession, row: SentinelActionProposal
    ) -> tuple[SentinelActionAdapter, Proposal, float]:
        proposal = Proposal.model_validate(row.payload)
        if proposal_fingerprint(proposal) != row.fingerprint:
            raise SentinelDenied("proposal_fingerprint_mismatch")
        incident = await self.store._incident(session, row.incident_id)
        book, control = await self.store._validate_proposal(session, proposal, incident)
        now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
        approvals = (
            await session.scalars(
                select(SentinelApproval).where(SentinelApproval.proposal_id == row.id).limit(17)
            )
        ).all()
        if len(approvals) > 16 or any(item.decision == "REJECTED" for item in approvals):
            raise SentinelDenied("approval_rejected")
        approved = any(
            approval_current(
                Approval(**{key: getattr(item, key) for key in Approval.model_fields}),
                proposal,
                now,
            )
            for item in approvals
        )
        if not risk_allowed(
            book,
            durable_approval=approved,
            mutable_actions_enabled=control.mutable_actions_enabled
            and self.store.settings.mutable_actions_enabled,
        ):
            raise SentinelDenied("risk_or_approval_denied")
        adapter = self.registry.resolve(book, proposal)
        return (
            adapter,
            proposal,
            min(book.timeout_seconds, self.store.settings.execution_timeout_seconds),
        )

    async def claim(self, proposal_id: UUID, owner_id: UUID) -> ExecutionClaim:
        async with self.store.database.transaction() as session:
            row = await self._lock_proposal(session, proposal_id)
            if row.state not in {"PROPOSED", "EXECUTING"}:
                raise SentinelDenied("proposal_not_claimable")
            await self._authorize(session, row)
            now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            execution = await session.scalar(
                select(SentinelExecution)
                .where(SentinelExecution.proposal_id == proposal_id)
                .with_for_update()
            )
            if execution is not None:
                if execution.dispatch_state != "CLAIMED":
                    raise SentinelDenied("dispatch_already_fenced")
                if execution.lease_expires_at > now:
                    raise SentinelConflict("execution_owned")
                if execution.owner_id == owner_id:
                    raise SentinelDenied("new_owner_identity_required")
                execution.owner_id = owner_id
                execution.execution_generation += 1
                execution.lease_expires_at = now + timedelta(
                    seconds=self.store.settings.lease_duration_seconds
                )
            else:
                count = await session.scalar(
                    select(func.count())
                    .select_from(SentinelExecution)
                    .where(SentinelExecution.dispatch_state.in_(("CLAIMED", "DISPATCHED")))
                )
                if count is None or count >= self.store.settings.max_active_executions:
                    raise SentinelDenied("active_execution_budget")
                execution = SentinelExecution(
                    id=uuid7(),
                    proposal_id=proposal_id,
                    execution_generation=1,
                    owner_id=owner_id,
                    lease_expires_at=now
                    + timedelta(seconds=self.store.settings.lease_duration_seconds),
                    idempotency_key=f"sentinel:{proposal_id}",
                    dispatch_state="CLAIMED",
                    started_at=now,
                )
                session.add(execution)
                row.state = "EXECUTING"
            await session.flush()
            return ExecutionClaim(
                proposal_id,
                execution.id,
                owner_id,
                execution.execution_generation,
                execution.idempotency_key,
            )

    async def _owned(self, session: AsyncSession, claim: ExecutionClaim) -> SentinelExecution:
        row = await session.scalar(
            select(SentinelExecution)
            .where(SentinelExecution.id == claim.execution_id)
            .with_for_update()
        )
        if row is None or (
            row.proposal_id,
            row.owner_id,
            row.execution_generation,
            row.idempotency_key,
        ) != (claim.proposal_id, claim.owner_id, claim.generation, claim.idempotency_key):
            raise SentinelDenied("stale_execution_owner")
        return row

    async def prepare_dispatch(
        self, claim: ExecutionClaim
    ) -> tuple[SentinelActionAdapter, DispatchRequest, float]:
        async with self.store.database.transaction() as session:
            proposal_row = await self._lock_proposal(session, claim.proposal_id)
            if proposal_row.state != "EXECUTING":
                raise SentinelDenied("proposal_not_executing")
            adapter, proposal, timeout = await self._authorize(session, proposal_row)
            execution = await self._owned(session, claim)
            now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            if execution.dispatch_state != "CLAIMED" or execution.lease_expires_at <= now:
                raise SentinelDenied("dispatch_lease_or_state_denied")
            execution.dispatch_state = "DISPATCHED"
            await session.flush()
            request = DispatchRequest(
                target_kind=proposal.target_kind,
                target_id=proposal.target_id,
                target_generation=proposal.target_generation,
                parameters=proposal.parameters,
                idempotency_key=claim.idempotency_key,
            )
            return adapter, request, timeout

    async def finish(
        self, claim: ExecutionClaim, outcome: str, result: ActionResult | None = None
    ) -> None:
        if outcome not in {"SUCCEEDED", "FAILED", "AMBIGUOUS"}:
            raise SentinelDenied("unknown_execution_outcome")
        async with self.store.database.transaction() as session:
            proposal = await self._lock_proposal(session, claim.proposal_id)
            execution = await self._owned(session, claim)
            if execution.dispatch_state != "DISPATCHED":
                raise SentinelDenied("execution_already_terminal")
            execution.dispatch_state = "AMBIGUOUS" if outcome == "AMBIGUOUS" else "COMPLETED"
            execution.completed_at = (
                await session.execute(select(func.clock_timestamp()))
            ).scalar_one()
            execution.result_classification = result.classification if result else outcome
            execution.external_reference = result.external_reference if result else None
            proposal.state = outcome

    async def dispatch(self, claim: ExecutionClaim) -> str:
        try:
            adapter, request, timeout = await self.prepare_dispatch(claim)
        except SentinelDenied:
            await self.reject_claim(claim)
            raise
        result = None
        outcome = "AMBIGUOUS"
        try:
            async with asyncio.timeout(timeout):
                result = await adapter.execute(request)
                if not isinstance(result, ActionResult):
                    raise SentinelDenied("invalid_adapter_result")
            outcome = "FAILED" if result.classification == "REJECTED" else "SUCCEEDED"
        except ProvenPreEffectRejection:
            result = None
            outcome = "FAILED"
        except Exception:
            result = None
            outcome = "AMBIGUOUS"
        await self.finish(claim, outcome, result)
        return outcome

    async def reject_claim(self, claim: ExecutionClaim) -> None:
        async with self.store.database.transaction() as session:
            proposal = await self._lock_proposal(session, claim.proposal_id)
            execution = await self._owned(session, claim)
            if execution.dispatch_state != "CLAIMED" or proposal.state != "EXECUTING":
                raise SentinelDenied("claim_not_rejectable")
            execution.dispatch_state = "COMPLETED"
            execution.completed_at = (
                await session.execute(select(func.clock_timestamp()))
            ).scalar_one()
            execution.result_classification = "PRE_DISPATCH_DENIED"
            proposal.state = "FAILED"

    async def cleanup(self) -> int:
        async with self.store.database.transaction() as session:
            cutoff = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            identities = list(
                (
                    await session.scalars(
                        select(SentinelExecution.proposal_id)
                        .where(
                            SentinelExecution.dispatch_state.in_(("CLAIMED", "DISPATCHED")),
                            SentinelExecution.lease_expires_at <= cutoff,
                        )
                        .order_by(SentinelExecution.lease_expires_at, SentinelExecution.proposal_id)
                        .limit(self.store.settings.cleanup_batch_size)
                    )
                ).all()
            )
        count = 0
        for identity in identities:
            async with self.store.database.transaction() as session:
                proposal = await self._lock_proposal(session, identity)
                execution = await session.scalar(
                    select(SentinelExecution)
                    .where(SentinelExecution.proposal_id == identity)
                    .with_for_update()
                )
                now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
                if (
                    execution is None
                    or execution.lease_expires_at > now
                    or execution.dispatch_state not in {"CLAIMED", "DISPATCHED"}
                ):
                    continue
                ambiguous = execution.dispatch_state == "DISPATCHED"
                execution.dispatch_state = "AMBIGUOUS" if ambiguous else "COMPLETED"
                execution.completed_at = now
                execution.result_classification = (
                    "AMBIGUOUS" if ambiguous else "PRE_DISPATCH_EXPIRED"
                )
                proposal.state = "AMBIGUOUS" if ambiguous else "FAILED"
                count += 1
        return count
