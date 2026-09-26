"""Trusted platform persistence seam. No public API, tenant scope or dispatch."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from types import MappingProxyType
from uuid import UUID, uuid7

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai.domain.sentinel.models import (
    SentinelActionProposal,
    SentinelApproval,
    SentinelControlState,
    SentinelFinding,
    SentinelIncident,
    SentinelRunbook,
    SentinelSignalReceipt,
)
from nexus_ai.sentinel.contracts import (
    Approval,
    Finding,
    IncidentState,
    Parameters,
    Proposal,
    Runbook,
    Signal,
    Subject,
    approval_current,
    canonical_json,
    digest,
    legal_transition,
    proposal_fingerprint,
)
from nexus_ai.sentinel.database import SentinelDatabase
from nexus_ai.sentinel.errors import SentinelConflict, SentinelDenied
from nexus_ai.sentinel.policy import risk_allowed


@dataclass(frozen=True)
class SourceBinding:
    adapter_id: str
    adapter_revision: int
    source_kind: str
    source_identity: str
    subject_kind: Subject
    subject_id: UUID
    organization_id: UUID | None = None


class SentinelStore:
    def __init__(
        self,
        database: SentinelDatabase,
        *,
        sources: Iterable[SourceBinding] = (),
        runbooks: Iterable[Runbook] = (),
    ) -> None:
        self.database = database
        self.settings = database.settings
        source_list = tuple(sources)
        runbook_list = tuple(runbooks)
        if len(source_list) > 256 or len(runbook_list) > 256:
            raise SentinelDenied("registry_bound_exceeded")
        self.sources = MappingProxyType(
            {(source.adapter_id, source.source_identity): source for source in source_list}
        )
        self.runbooks = MappingProxyType({book.id: book for book in runbook_list})
        if len(self.sources) != len(source_list) or len(self.runbooks) != len(runbook_list):
            raise SentinelDenied("duplicate_source_registration")
        if any(
            book.parameter_schema_digest != digest(Parameters.model_json_schema())
            for book in runbook_list
        ):
            raise SentinelDenied("unregistered_parameter_schema")

    async def ingest(self, signal: Signal) -> tuple[UUID, UUID]:
        binding = SourceBinding(
            signal.adapter_id,
            signal.adapter_revision,
            signal.source_kind,
            signal.source_identity,
            signal.subject_kind,
            signal.subject_id,
            signal.organization_id,
        )
        if self.sources.get((signal.adapter_id, signal.source_identity)) != binding:
            raise SentinelDenied("unregistered_source_subject")
        payload = signal.model_dump(mode="json")
        if (
            len(canonical_json(payload).encode()) > self.settings.max_signal_bytes
            or len(signal.facts.model_dump_json().encode()) > self.settings.max_facts_bytes
            or len(signal.evidence_refs) > self.settings.max_evidence_refs
        ):
            raise SentinelDenied("signal_budget_exceeded")
        semantic_digest = digest(payload)
        async with self.database.transaction() as session:
            receipt_id = await session.scalar(
                insert(SentinelSignalReceipt)
                .values(
                    id=uuid7(),
                    adapter_id=signal.adapter_id,
                    adapter_revision=signal.adapter_revision,
                    source_identity=signal.source_identity,
                    source_observation_id=signal.source_observation_id,
                    semantic_digest=semantic_digest,
                    payload=payload,
                    schema_version=1,
                    status="RECORDED",
                )
                .on_conflict_do_nothing()
                .returning(SentinelSignalReceipt.id)
            )
            if receipt_id is None:
                existing = (
                    await session.scalars(
                        select(SentinelSignalReceipt).where(
                            SentinelSignalReceipt.adapter_id == signal.adapter_id,
                            SentinelSignalReceipt.source_identity == signal.source_identity,
                            SentinelSignalReceipt.source_observation_id
                            == signal.source_observation_id,
                        )
                    )
                ).one()
                if existing.semantic_digest != semantic_digest or existing.incident_id is None:
                    raise SentinelConflict("observation_identity_conflict")
                return existing.id, existing.incident_id
            correlation = digest(
                [
                    signal.source_kind,
                    signal.subject_kind,
                    str(signal.subject_id),
                    str(signal.organization_id),
                    signal.fingerprint,
                ]
            )
            incident_id = await session.scalar(
                insert(SentinelIncident)
                .values(
                    id=uuid7(),
                    correlation_key=correlation,
                    subject_kind=signal.subject_kind,
                    subject_id=signal.subject_id,
                    organization_id=signal.organization_id,
                    state="OPEN",
                    severity=signal.severity,
                    revision=1,
                    summary=signal.facts.condition,
                )
                .on_conflict_do_nothing()
                .returning(SentinelIncident.id)
            )
            if incident_id is None:
                incident = (
                    await session.scalars(
                        select(SentinelIncident)
                        .where(SentinelIncident.correlation_key == correlation)
                        .with_for_update()
                    )
                ).one()
                incident_id = incident.id
                incident.revision += 1
                incident.last_seen_at = (
                    await session.execute(select(func.clock_timestamp()))
                ).scalar_one()
            await session.execute(
                update(SentinelSignalReceipt)
                .where(SentinelSignalReceipt.id == receipt_id)
                .values(status="CORRELATED", incident_id=incident_id)
            )
            return receipt_id, incident_id

    async def transition(
        self,
        incident_id: UUID,
        expected_revision: int,
        state: IncidentState,
        *,
        resolution_source: str | None = None,
    ) -> int:
        async with self.database.transaction() as session:
            incident = await self._incident(session, incident_id)
            if incident.revision != expected_revision:
                raise SentinelConflict("incident_revision_conflict")
            if not legal_transition(IncidentState(incident.state), state, resolution_source):
                raise SentinelDenied("illegal_incident_transition")
            incident.state = state
            incident.revision += 1
            if resolution_source is not None:
                incident.resolution_source = resolution_source
            return incident.revision

    async def add_finding(self, finding: Finding) -> UUID:
        if len(finding.evidence_refs) > self.settings.max_evidence_refs:
            raise SentinelDenied("finding_evidence_budget")
        async with self.database.transaction() as session:
            await self._incident(session, finding.incident_id)
            references = set(finding.evidence_refs)
            known = set(
                (
                    await session.scalars(
                        select(SentinelSignalReceipt.id).where(
                            SentinelSignalReceipt.incident_id == finding.incident_id,
                            SentinelSignalReceipt.id.in_(references),
                        )
                    )
                ).all()
            )
            if known != references:
                raise SentinelDenied("finding_evidence_not_bound_to_incident")
            count = await session.scalar(
                select(func.count())
                .select_from(SentinelFinding)
                .where(SentinelFinding.incident_id == finding.incident_id)
            )
            if count is None or count >= self.settings.max_findings_per_incident:
                raise SentinelDenied("finding_budget_exceeded")
            identity = uuid7()
            session.add(
                SentinelFinding(
                    id=identity,
                    incident_id=finding.incident_id,
                    revision=count + 1,
                    payload=finding.model_dump(mode="json"),
                )
            )
            return identity

    async def register_runbook(self, book: Runbook) -> UUID:
        if self.runbooks.get(book.id) != book:
            raise SentinelDenied("unregistered_runbook")
        payload = book.model_dump(mode="json")
        semantic_digest = digest(payload)
        async with self.database.transaction() as session:
            identity = await session.scalar(
                insert(SentinelRunbook)
                .values(
                    id=book.id,
                    key=book.key,
                    revision=book.revision,
                    handler_key=book.handler_key,
                    risk=book.risk,
                    definition=payload,
                    semantic_digest=semantic_digest,
                )
                .on_conflict_do_nothing()
                .returning(SentinelRunbook.id)
            )
            if identity is None:
                existing = (
                    await session.scalars(
                        select(SentinelRunbook).where(
                            SentinelRunbook.key == book.key,
                            SentinelRunbook.revision == book.revision,
                        )
                    )
                ).one_or_none()
                if existing is None or existing.semantic_digest != semantic_digest:
                    raise SentinelConflict("immutable_runbook_conflict")
                return existing.id
            return identity

    async def propose(self, proposal: Proposal) -> UUID:
        fingerprint = proposal_fingerprint(proposal)
        async with self.database.transaction() as session:
            incident = await self._incident(session, proposal.incident_id)
            existing = await session.scalar(
                select(SentinelActionProposal.id).where(
                    SentinelActionProposal.fingerprint == fingerprint
                )
            )
            if existing is not None:
                return existing
            await self._validate_proposal(session, proposal, incident)
            count = await session.scalar(
                select(func.count())
                .select_from(SentinelActionProposal)
                .where(SentinelActionProposal.incident_id == proposal.incident_id)
            )
            if count is None or count >= self.settings.max_proposals_per_incident:
                raise SentinelDenied("proposal_budget_exceeded")
            identity = uuid7()
            session.add(
                SentinelActionProposal(
                    id=identity,
                    incident_id=proposal.incident_id,
                    incident_revision=proposal.incident_revision,
                    runbook_id=proposal.runbook_id,
                    runbook_revision=proposal.runbook_revision,
                    policy_revision=proposal.policy_revision,
                    fingerprint=fingerprint,
                    payload=proposal.model_dump(mode="json"),
                    state="PROPOSED",
                    expires_at=proposal.expires_at,
                )
            )
            return identity

    async def record_approval(self, approval: Approval) -> UUID:
        """Persist a decision from trusted operator admission; never dispatch an action."""
        async with self.database.transaction() as session:
            row = await session.get(SentinelActionProposal, approval.proposal_id)
            if row is None:
                raise SentinelDenied("proposal_not_found")
            incident = await self._incident(session, row.incident_id)
            proposal = Proposal.model_validate(row.payload)
            await self._validate_proposal(session, proposal, incident)
            now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            if (
                approval.proposal_fingerprint != row.fingerprint
                or approval.policy_revision != row.policy_revision
                or not now < approval.expires_at <= row.expires_at
            ):
                raise SentinelDenied("approval_binding_invalid")
            count = await session.scalar(
                select(func.count())
                .select_from(SentinelApproval)
                .where(
                    SentinelApproval.proposal_id == approval.proposal_id,
                    SentinelApproval.approver_principal != approval.approver_principal,
                )
            )
            if count is None or count >= 16:
                raise SentinelDenied("approval_budget_exceeded")
            identity = await session.scalar(
                insert(SentinelApproval)
                .values(id=uuid7(), **approval.model_dump())
                .on_conflict_do_nothing()
                .returning(SentinelApproval.id)
            )
            if identity is None:
                existing = (
                    await session.scalars(
                        select(SentinelApproval).where(
                            SentinelApproval.proposal_id == approval.proposal_id,
                            SentinelApproval.approver_principal == approval.approver_principal,
                        )
                    )
                ).one()
                if any(
                    getattr(existing, key) != value for key, value in approval.model_dump().items()
                ):
                    raise SentinelConflict("approval_identity_conflict")
                return existing.id
            return identity

    async def eligible(self, proposal_id: UUID, *, target_generation: int | None) -> bool:
        """Foundation policy assessment only; executor must later revalidate atomically."""
        async with self.database.transaction() as session:
            row = await session.get(SentinelActionProposal, proposal_id)
            if row is None or row.state != "PROPOSED":
                return False
            incident = await self._incident(session, row.incident_id)
            proposal = Proposal.model_validate(row.payload)
            if target_generation != proposal.target_generation:
                return False
            try:
                book, control = await self._validate_proposal(session, proposal, incident)
            except SentinelDenied:
                return False
            now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            approvals = (
                await session.scalars(
                    select(SentinelApproval)
                    .where(SentinelApproval.proposal_id == proposal_id)
                    .limit(17)
                )
            ).all()
            if len(approvals) > 16 or any(item.decision == "REJECTED" for item in approvals):
                return False
            approved = any(
                approval_current(
                    Approval(**{field: getattr(item, field) for field in Approval.model_fields}),
                    proposal,
                    now,
                )
                for item in approvals
            )
            return risk_allowed(
                book,
                durable_approval=approved,
                mutable_actions_enabled=control.mutable_actions_enabled
                and self.settings.mutable_actions_enabled,
            )

    async def set_mutable_actions(self, expected_revision: int, *, enabled: bool) -> int:
        async with self.database.transaction() as session:
            revision = await session.scalar(
                update(SentinelControlState)
                .where(
                    SentinelControlState.id == 1,
                    SentinelControlState.revision == expected_revision,
                )
                .values(
                    revision=expected_revision + 1,
                    mutable_actions_enabled=enabled,
                    updated_at=func.clock_timestamp(),
                )
                .returning(SentinelControlState.revision)
            )
            if revision is None:
                raise SentinelConflict("policy_revision_conflict")
            return revision

    async def list_incidents(self, *, after: UUID | None = None, limit: int = 50) -> list[UUID]:
        if not 1 <= limit <= self.settings.max_incidents_scanned:
            raise SentinelDenied("incident_scan_budget")
        async with self.database.transaction() as session:
            query = select(SentinelIncident.id).order_by(SentinelIncident.id).limit(limit)
            if after is not None:
                query = query.where(SentinelIncident.id > after)
            return list((await session.scalars(query)).all())

    async def _incident(self, session: AsyncSession, identity: UUID) -> SentinelIncident:
        row = await session.scalar(
            select(SentinelIncident).where(SentinelIncident.id == identity).with_for_update()
        )
        if row is None:
            raise SentinelDenied("incident_not_found")
        return row

    async def _validate_proposal(
        self, session: AsyncSession, proposal: Proposal, incident: SentinelIncident
    ) -> tuple[Runbook, SentinelControlState]:
        book = self.runbooks.get(proposal.runbook_id)
        stored = await session.get(SentinelRunbook, proposal.runbook_id)
        control = await session.get(SentinelControlState, 1, with_for_update={"read": True})
        now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
        if (
            book is None
            or stored is None
            or control is None
            or stored.semantic_digest != digest(book.model_dump(mode="json"))
            or not book.enabled
            or book.revision != proposal.runbook_revision
            or book.risk != proposal.risk
            or proposal.target_kind not in book.target_kinds
            or proposal.target_kind != incident.subject_kind
            or proposal.target_id != incident.subject_id
            or (proposal.target_kind == Subject.ORGANIZATION and not book.non_mutating)
            or incident.revision != proposal.incident_revision
            or incident.state in {"RESOLVED", "CLOSED"}
            or control.revision != proposal.policy_revision
            or not now < proposal.expires_at <= now + timedelta(hours=1)
        ):
            raise SentinelDenied("stale_or_invalid_proposal")
        latest = await session.scalar(
            select(func.max(SentinelRunbook.revision)).where(SentinelRunbook.key == book.key)
        )
        if latest != book.revision:
            raise SentinelDenied("superseded_runbook")
        return book, control
