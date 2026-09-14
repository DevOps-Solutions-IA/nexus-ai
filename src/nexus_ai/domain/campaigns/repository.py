"""Tenant-scoped PostgreSQL Campaign repository and fencing primitives."""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Select, and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from nexus_ai.campaigns.entities import (
    AudienceSnapshot,
    AudienceSourceType,
    Campaign,
    CampaignDraft,
    CampaignRecipient,
    CampaignRevision,
    CampaignRun,
    CampaignTransition,
    EligibilityReason,
    RecipientAttempt,
    RecipientClaim,
    SendPermit,
    SuppressionRequest,
    ThrottlePolicy,
)
from nexus_ai.campaigns.errors import CampaignExecutionFencedError, CampaignInvalidStateError
from nexus_ai.campaigns.identity import downstream_key, recipient_identity, semantic_digest
from nexus_ai.campaigns.state_machine import (
    CampaignRunState,
    CampaignState,
    RecipientAttemptState,
    RecipientState,
    SendPermitState,
    SnapshotState,
    require_campaign_transition,
    resolve_attempt_transition,
)
from nexus_ai.domain.campaigns.models import (
    CampaignAudienceSnapshotRecord,
    CampaignContactPreferenceRecord,
    CampaignOrganizationThrottleWindowRecord,
    CampaignPolicyEpochRecord,
    CampaignRecipientAttemptRecord,
    CampaignRecipientRecord,
    CampaignRecord,
    CampaignRevisionRecord,
    CampaignRunRecord,
    CampaignSendPermitRecord,
    CampaignSuppressionRecord,
    CampaignThrottleWindowRecord,
    CampaignTransitionHistoryRecord,
)
from nexus_ai.domain.customers.models import (
    ConversationRecord,
    CustomerIdentityRecord,
    CustomerRecord,
)
from nexus_ai.infrastructure.tenant_session import TenantSession


def _campaign(row: CampaignRecord) -> Campaign:
    return Campaign(
        id=row.id,
        organization_id=row.organization_id,
        campaign_key=row.campaign_key,
        name=row.name,
        state=row.state,
        revision=row.revision,
        draft=CampaignDraft.model_validate(row.draft_specification),
        prepared_revision_id=row.prepared_revision_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _revision(row: CampaignRevisionRecord) -> CampaignRevision:
    return CampaignRevision(
        id=row.id,
        organization_id=row.organization_id,
        campaign_id=row.campaign_id,
        revision_number=row.revision_number,
        content_hash=row.content_hash,
        specification=CampaignDraft.model_validate(row.specification),
        published_at=row.published_at,
    )


def _snapshot(row: CampaignAudienceSnapshotRecord) -> AudienceSnapshot:
    return AudienceSnapshot(
        id=row.id,
        organization_id=row.organization_id,
        campaign_revision_id=row.campaign_revision_id,
        state=row.state,
        source_type=row.source_type,
        source_digest=row.source_digest,
        cursor=row.cursor,
        resolved_count=row.resolved_count,
        eligible_count=row.eligible_count,
        rejected_count=row.rejected_count,
        sealed_at=row.sealed_at,
        created_at=row.created_at,
    )


def _recipient(row: CampaignRecipientRecord) -> CampaignRecipient:
    return CampaignRecipient(
        id=row.id,
        organization_id=row.organization_id,
        snapshot_id=row.snapshot_id,
        customer_id=row.customer_id,
        identity_id=row.identity_id,
        conversation_id=row.conversation_id,
        channel=row.channel,
        destination_fingerprint=row.destination_fingerprint,
        state=row.state,
        eligibility_reason=row.eligibility_reason,
        next_eligible_at=row.next_eligible_at,
        evaluated_consent_epoch=row.evaluated_consent_epoch,
        evaluated_suppression_epoch=row.evaluated_suppression_epoch,
        created_at=row.created_at,
    )


def _run(row: CampaignRunRecord) -> CampaignRun:
    return CampaignRun(
        id=row.id,
        organization_id=row.organization_id,
        campaign_id=row.campaign_id,
        campaign_revision_id=row.campaign_revision_id,
        audience_snapshot_id=row.audience_snapshot_id,
        state=row.state,
        idempotency_key=row.idempotency_key,
        release_workflow_run_id=row.release_workflow_run_id,
        release_schedule_occurrence_id=row.release_schedule_occurrence_id,
        schedule_id=row.schedule_id,
        claimed_count=row.claimed_count,
        dispatched_count=row.dispatched_count,
        suppressed_count=row.suppressed_count,
        failed_count=row.failed_count,
        authorized_count=row.authorized_count,
        attempt_materialization_cursor=row.attempt_materialization_cursor,
        attempt_materialization_complete=row.attempt_materialization_complete,
        cancellation_processed_count=row.cancellation_processed_count,
        cancellation_complete=row.cancellation_complete,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _attempt(row: CampaignRecipientAttemptRecord) -> RecipientAttempt:
    return RecipientAttempt(
        id=row.id,
        organization_id=row.organization_id,
        campaign_run_id=row.campaign_run_id,
        recipient_id=row.recipient_id,
        state=row.state,
        attempt_number=row.attempt_number,
        owner_id=row.owner_id,
        claim_token=row.claim_token,
        claimed_at=row.claimed_at,
        p14_idempotency_key=row.p14_idempotency_key,
        workflow_run_id=row.workflow_run_id,
        p09_idempotency_key=row.p09_idempotency_key,
        message_id=row.message_id,
        error_code=row.error_code,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _permit(row: CampaignSendPermitRecord) -> SendPermit:
    return SendPermit(
        id=row.id,
        organization_id=row.organization_id,
        recipient_attempt_id=row.recipient_attempt_id,
        state=row.state,
        permit_token=row.permit_token,
        consent_epoch=row.consent_epoch,
        suppression_epoch=row.suppression_epoch,
        p09_idempotency_key=row.p09_idempotency_key,
        authorized_at=row.authorized_at,
        consumed_at=row.consumed_at,
        message_id=row.message_id,
    )


class CampaignRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self.session = tenant.session
        self.organization_id = tenant.organization_id

    async def database_now(self) -> dt.datetime:
        value = (await self.session.execute(select(func.clock_timestamp()))).scalar_one()
        if not isinstance(value, dt.datetime):
            raise RuntimeError("database clock returned an invalid value")
        return value

    async def create_campaign(self, campaign_key: str, name: str, draft: CampaignDraft) -> Campaign:
        row = CampaignRecord(
            id=uuid.uuid7(),
            organization_id=self.organization_id,
            campaign_key=campaign_key,
            name=name,
            state=CampaignState.DRAFT.value,
            revision=1,
            draft_specification=draft.model_dump(mode="json"),
            prepared_revision_id=None,
        )
        self.session.add(row)
        await self.session.flush()
        await self.transition("CAMPAIGN", row.id, None, row.state, "CAMPAIGN_CREATED")
        return _campaign(row)

    async def campaign_row(
        self, campaign_id: uuid.UUID, *, for_update: bool = False
    ) -> CampaignRecord | None:
        query: Select[tuple[CampaignRecord]] = select(CampaignRecord).where(
            CampaignRecord.organization_id == self.organization_id, CampaignRecord.id == campaign_id
        )
        if for_update:
            query = query.with_for_update()
        return (await self.session.execute(query)).scalar_one_or_none()

    async def campaign(self, campaign_id: uuid.UUID) -> Campaign | None:
        row = await self.campaign_row(campaign_id)
        return None if row is None else _campaign(row)

    async def list_campaigns(self, *, limit: int, offset: int) -> list[Campaign]:
        rows = (
            (
                await self.session.execute(
                    select(CampaignRecord)
                    .where(CampaignRecord.organization_id == self.organization_id)
                    .order_by(CampaignRecord.created_at.desc(), CampaignRecord.id)
                    .offset(offset)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_campaign(row) for row in rows]

    async def update_campaign(
        self, row: CampaignRecord, *, name: str | None, draft: CampaignDraft | None
    ) -> Campaign:
        if CampaignState(row.state) is not CampaignState.DRAFT:
            raise CampaignInvalidStateError("only DRAFT campaigns may be edited")
        if name is not None:
            row.name = name
        if draft is not None:
            row.draft_specification = draft.model_dump(mode="json")
        row.revision += 1
        row.updated_at = await self.database_now()
        await self.session.flush()
        return _campaign(row)

    async def begin_preparation(
        self, row: CampaignRecord
    ) -> tuple[CampaignRevision, AudienceSnapshot]:
        require_campaign_transition(CampaignState(row.state), CampaignState.PREPARING)
        previous = row.state
        row.state = CampaignState.PREPARING.value
        draft = CampaignDraft.model_validate(row.draft_specification)
        digest = semantic_digest(draft.model_dump(mode="json"))
        revision = CampaignRevisionRecord(
            id=uuid.uuid7(),
            organization_id=self.organization_id,
            campaign_id=row.id,
            revision_number=row.revision,
            content_hash=digest,
            release_workflow_version_id=draft.release_workflow_version_id,
            recipient_workflow_version_id=draft.recipient_workflow_version_id,
            account_id=draft.account_id,
            channel=draft.channel.value,
            specification=draft.model_dump(mode="json"),
        )
        snapshot = CampaignAudienceSnapshotRecord(
            id=uuid.uuid7(),
            organization_id=self.organization_id,
            campaign_revision_id=revision.id,
            state=SnapshotState.OPEN.value,
            source_type=draft.audience_source.value,
            source_reference=draft.source_reference,
            source_digest=semantic_digest(
                {"source": draft.model_dump(mode="json").get("audience")}
            ),
            cursor=0,
            resolved_count=0,
            eligible_count=0,
            rejected_count=0,
        )
        self.session.add(revision)
        await self.session.flush()
        self.session.add(snapshot)
        await self.transition("CAMPAIGN", row.id, previous, row.state, "CAMPAIGN_PREPARING")
        await self.session.flush()
        return _revision(revision), _snapshot(snapshot)

    async def revision_row(self, revision_id: uuid.UUID) -> CampaignRevisionRecord | None:
        return (
            await self.session.execute(
                select(CampaignRevisionRecord).where(
                    CampaignRevisionRecord.organization_id == self.organization_id,
                    CampaignRevisionRecord.id == revision_id,
                )
            )
        ).scalar_one_or_none()

    async def active_preparation(
        self, campaign_id: uuid.UUID, revision_number: int
    ) -> tuple[CampaignRevision, AudienceSnapshot]:
        revision = (
            await self.session.execute(
                select(CampaignRevisionRecord).where(
                    CampaignRevisionRecord.organization_id == self.organization_id,
                    CampaignRevisionRecord.campaign_id == campaign_id,
                    CampaignRevisionRecord.revision_number == revision_number,
                )
            )
        ).scalar_one_or_none()
        if revision is None:
            raise CampaignInvalidStateError("campaign preparation revision is absent")
        snapshot = await self.snapshot_row(revision.id, for_update=True)
        if snapshot is None or snapshot.state != SnapshotState.OPEN.value:
            raise CampaignInvalidStateError("campaign preparation snapshot is not resumable")
        return _revision(revision), _snapshot(snapshot)

    async def snapshot_row(
        self, revision_id: uuid.UUID, *, for_update: bool = False
    ) -> CampaignAudienceSnapshotRecord | None:
        query: Select[tuple[CampaignAudienceSnapshotRecord]] = select(
            CampaignAudienceSnapshotRecord
        ).where(
            CampaignAudienceSnapshotRecord.organization_id == self.organization_id,
            CampaignAudienceSnapshotRecord.campaign_revision_id == revision_id,
        )
        if for_update:
            query = query.with_for_update()
        return (await self.session.execute(query)).scalar_one_or_none()

    async def materialize_batch(self, revision_id: uuid.UUID, *, limit: int) -> AudienceSnapshot:
        revision = await self.revision_row(revision_id)
        snapshot = await self.snapshot_row(revision_id, for_update=True)
        if revision is None or snapshot is None or snapshot.state != SnapshotState.OPEN.value:
            raise CampaignInvalidStateError("campaign audience is not open for materialization")
        spec = CampaignDraft.model_validate(revision.specification)
        if spec.audience_source is not AudienceSourceType.EXPLICIT_CUSTOMERS:
            raise CampaignInvalidStateError("governed segment/import resolver is not configured")
        members = spec.audience[snapshot.cursor : snapshot.cursor + limit]
        for member in members:
            reason, next_at, consent_epoch, suppression_epoch, destination = await self.eligibility(
                revision.campaign_id,
                member.customer_id,
                member.identity_id,
                member.conversation_id,
                spec.channel.value,
                spec.quiet_hours,
            )
            state = RecipientState.ELIGIBLE
            if reason is EligibilityReason.DEFERRED_QUIET_HOURS:
                state = RecipientState.DEFERRED
            elif reason is not EligibilityReason.ELIGIBLE:
                state = RecipientState.SUPPRESSED
            recipient = CampaignRecipientRecord(
                id=uuid.uuid7(),
                organization_id=self.organization_id,
                snapshot_id=snapshot.id,
                customer_id=member.customer_id,
                identity_id=member.identity_id,
                conversation_id=member.conversation_id,
                channel=spec.channel.value,
                destination_fingerprint=hashlib.sha256(destination.encode()).hexdigest(),
                state=state.value,
                eligibility_reason=reason.value,
                next_eligible_at=next_at,
                evaluated_consent_epoch=consent_epoch,
                evaluated_suppression_epoch=suppression_epoch,
            )
            self.session.add(recipient)
            snapshot.resolved_count += 1
            if state is RecipientState.ELIGIBLE:
                snapshot.eligible_count += 1
            else:
                snapshot.rejected_count += 1
        snapshot.cursor += len(members)
        await self.session.flush()
        return _snapshot(snapshot)

    async def seal_preparation(self, campaign_id: uuid.UUID, revision_id: uuid.UUID) -> Campaign:
        campaign = await self.campaign_row(campaign_id, for_update=True)
        snapshot = await self.snapshot_row(revision_id, for_update=True)
        if campaign is None or snapshot is None or campaign.state != CampaignState.PREPARING.value:
            raise CampaignInvalidStateError("campaign preparation is not active")
        revision = await self.revision_row(revision_id)
        if revision is None:
            raise CampaignInvalidStateError("campaign revision is absent")
        spec = CampaignDraft.model_validate(revision.specification)
        if snapshot.cursor != len(spec.audience):
            raise CampaignInvalidStateError("audience materialization is incomplete")
        snapshot.state = SnapshotState.SEALED.value
        snapshot.sealed_at = await self.database_now()
        previous = campaign.state
        campaign.state = CampaignState.READY.value
        campaign.prepared_revision_id = revision_id
        await self.transition(
            "CAMPAIGN", campaign.id, previous, campaign.state, "CAMPAIGN_PREPARED"
        )
        await self.session.flush()
        await self.session.refresh(campaign)
        return _campaign(campaign)

    async def audience(
        self, campaign_id: uuid.UUID, *, limit: int, offset: int
    ) -> list[CampaignRecipient]:
        rows = (
            (
                await self.session.execute(
                    select(CampaignRecipientRecord)
                    .join(
                        CampaignAudienceSnapshotRecord,
                        and_(
                            CampaignAudienceSnapshotRecord.id
                            == CampaignRecipientRecord.snapshot_id,
                            CampaignAudienceSnapshotRecord.organization_id
                            == CampaignRecipientRecord.organization_id,
                        ),
                    )
                    .join(
                        CampaignRevisionRecord,
                        and_(
                            CampaignRevisionRecord.id
                            == CampaignAudienceSnapshotRecord.campaign_revision_id,
                            CampaignRevisionRecord.organization_id
                            == CampaignAudienceSnapshotRecord.organization_id,
                        ),
                    )
                    .where(CampaignRevisionRecord.campaign_id == campaign_id)
                    .order_by(CampaignRecipientRecord.created_at, CampaignRecipientRecord.id)
                    .offset(offset)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_recipient(row) for row in rows]

    async def create_run(
        self,
        campaign: CampaignRecord,
        *,
        idempotency_key: str,
        schedule_id: uuid.UUID | None = None,
    ) -> CampaignRun:
        if campaign.prepared_revision_id is None or campaign.state not in {
            CampaignState.READY.value,
            CampaignState.SCHEDULED.value,
        }:
            raise CampaignInvalidStateError("campaign must be READY or SCHEDULED")
        snapshot = await self.snapshot_row(campaign.prepared_revision_id)
        if snapshot is None or snapshot.state != SnapshotState.SEALED.value:
            raise CampaignInvalidStateError("campaign audience is not sealed")
        existing = (
            await self.session.execute(
                select(CampaignRunRecord).where(
                    CampaignRunRecord.organization_id == self.organization_id,
                    CampaignRunRecord.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return _run(existing)
        row = CampaignRunRecord(
            id=uuid.uuid7(),
            organization_id=self.organization_id,
            campaign_id=campaign.id,
            campaign_revision_id=campaign.prepared_revision_id,
            audience_snapshot_id=snapshot.id,
            state=CampaignRunState.PENDING_RELEASE.value,
            idempotency_key=idempotency_key,
            schedule_id=schedule_id,
            claimed_count=0,
            dispatched_count=0,
            suppressed_count=0,
            failed_count=0,
            authorized_count=0,
            attempt_materialization_cursor=0,
            attempt_materialization_complete=False,
            cancellation_processed_count=0,
            cancellation_complete=False,
        )
        self.session.add(row)
        await self.session.flush()
        await self.transition("RUN", row.id, None, row.state, "CAMPAIGN_RUN_CREATED")
        return _run(row)

    async def run_row(
        self, run_id: uuid.UUID, *, for_update: bool = False
    ) -> CampaignRunRecord | None:
        query: Select[tuple[CampaignRunRecord]] = select(CampaignRunRecord).where(
            CampaignRunRecord.organization_id == self.organization_id,
            CampaignRunRecord.id == run_id,
        )
        if for_update:
            query = query.with_for_update()
        return (await self.session.execute(query)).scalar_one_or_none()

    async def set_release_run(self, run_id: uuid.UUID, workflow_run_id: uuid.UUID) -> CampaignRun:
        row = await self.run_row(run_id, for_update=True)
        if row is None or row.state != CampaignRunState.PENDING_RELEASE.value:
            raise CampaignInvalidStateError("campaign release is not pending")
        if row.release_workflow_run_id not in {None, workflow_run_id}:
            raise CampaignExecutionFencedError("release workflow identity changed")
        row.release_workflow_run_id = workflow_run_id
        await self.session.flush()
        await self.session.refresh(row)
        return _run(row)

    async def bind_scheduled_release(
        self,
        run_id: uuid.UUID,
        *,
        schedule_id: uuid.UUID,
        occurrence_id: uuid.UUID,
        workflow_run_id: uuid.UUID,
        workflow_version_id: uuid.UUID,
    ) -> CampaignRun:
        observed = await self.run_row(run_id)
        if observed is None:
            raise CampaignInvalidStateError("campaign run is absent")
        campaign = await self.campaign_row(observed.campaign_id, for_update=True)
        row = await self.run_row(run_id, for_update=True)
        if row is None or campaign is None:
            raise CampaignInvalidStateError("campaign release binding is absent")
        existing = (row.release_schedule_occurrence_id, row.release_workflow_run_id)
        incoming = (occurrence_id, workflow_run_id)
        if existing == incoming:
            return _run(row)
        if existing != (None, None):
            raise CampaignExecutionFencedError("scheduled release identity changed")
        if row.state != CampaignRunState.PENDING_RELEASE.value:
            raise CampaignInvalidStateError("campaign release is not pending")
        if campaign.state != CampaignState.SCHEDULED.value:
            raise CampaignInvalidStateError("campaign does not authorize scheduled release binding")
        if row.schedule_id != schedule_id:
            raise CampaignExecutionFencedError("scheduled release schedule identity changed")
        revision = await self.revision_row(row.campaign_revision_id)
        if revision is None or revision.release_workflow_version_id != workflow_version_id:
            raise CampaignExecutionFencedError("scheduled release workflow version changed")
        row.release_schedule_occurrence_id = occurrence_id
        row.release_workflow_run_id = workflow_run_id
        await self.transition(
            "RUN",
            row.id,
            row.state,
            row.state,
            "SCHEDULED_RELEASE_BOUND",
        )
        await self.session.flush()
        await self.session.refresh(row)
        return _run(row)

    async def bind_schedule(self, run_id: uuid.UUID, schedule_id: uuid.UUID) -> CampaignRun:
        row = await self.run_row(run_id, for_update=True)
        if row is None:
            raise CampaignInvalidStateError("campaign run is absent")
        if row.schedule_id not in {None, schedule_id}:
            raise CampaignExecutionFencedError("campaign run already has another schedule")
        row.schedule_id = schedule_id
        await self.session.flush()
        await self.session.refresh(row)
        return _run(row)

    async def mark_campaign_scheduled(self, campaign_id: uuid.UUID) -> Campaign:
        campaign = await self.campaign_row(campaign_id, for_update=True)
        if campaign is None:
            raise CampaignInvalidStateError("campaign does not exist")
        if campaign.state == CampaignState.SCHEDULED.value:
            return _campaign(campaign)
        previous = CampaignState(campaign.state)
        require_campaign_transition(previous, CampaignState.SCHEDULED)
        campaign.state = CampaignState.SCHEDULED.value
        campaign.revision += 1
        await self.transition(
            "CAMPAIGN", campaign.id, previous.value, campaign.state, "CAMPAIGN_SCHEDULED"
        )
        await self.session.flush()
        await self.session.refresh(campaign)
        return _campaign(campaign)

    async def begin_run_activation(self, run_id: uuid.UUID) -> CampaignRun:
        observed = await self.run_row(run_id)
        if observed is None:
            raise CampaignInvalidStateError("campaign run is absent")
        campaign = await self.campaign_row(observed.campaign_id, for_update=True)
        row = await self.run_row(run_id, for_update=True)
        if row is None:
            raise CampaignInvalidStateError("campaign run is absent")
        if row.state == CampaignRunState.MATERIALIZING.value:
            if campaign is None or campaign.state != CampaignState.RUNNING.value:
                raise CampaignInvalidStateError("campaign does not authorize materialization")
            return _run(row)
        if row.state == CampaignRunState.RUNNING.value:
            return _run(row)
        if row.state != CampaignRunState.PENDING_RELEASE.value:
            raise CampaignInvalidStateError("campaign release is not pending")
        if campaign is None or campaign.state not in {
            CampaignState.READY.value,
            CampaignState.SCHEDULED.value,
        }:
            raise CampaignInvalidStateError("campaign cannot start")
        previous = campaign.state
        campaign.state = CampaignState.RUNNING.value
        row.state = CampaignRunState.MATERIALIZING.value
        await self.transition("CAMPAIGN", campaign.id, previous, campaign.state, "CAMPAIGN_STARTED")
        await self.transition(
            "RUN",
            row.id,
            CampaignRunState.PENDING_RELEASE.value,
            row.state,
            "ATTEMPT_MATERIALIZATION_STARTED",
        )
        await self.session.flush()
        await self.session.refresh(row)
        return _run(row)

    async def materialize_attempt_batch(
        self, run_id: uuid.UUID, *, limit: int
    ) -> tuple[CampaignRun, int]:
        if limit < 1:
            raise ValueError("attempt materialization limit must be positive")
        run = await self.run_row(run_id, for_update=True)
        if run is None or run.state != CampaignRunState.MATERIALIZING.value:
            raise CampaignInvalidStateError("campaign run is not materializing attempts")
        recipients = (
            (
                await self.session.execute(
                    select(CampaignRecipientRecord)
                    .where(
                        CampaignRecipientRecord.organization_id == self.organization_id,
                        CampaignRecipientRecord.snapshot_id == run.audience_snapshot_id,
                        CampaignRecipientRecord.state.in_(
                            [RecipientState.ELIGIBLE.value, RecipientState.DEFERRED.value]
                        ),
                    )
                    .order_by(CampaignRecipientRecord.created_at, CampaignRecipientRecord.id)
                    .offset(run.attempt_materialization_cursor)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        revision = await self.revision_row(run.campaign_revision_id)
        if revision is None:
            raise CampaignInvalidStateError("campaign revision is absent")
        for recipient in recipients:
            logical = recipient_identity(
                self.organization_id,
                run.campaign_id,
                revision.revision_number,
                run.id,
                recipient.id,
                recipient.channel,
            )
            self.session.add(
                CampaignRecipientAttemptRecord(
                    id=uuid.uuid7(),
                    organization_id=self.organization_id,
                    campaign_run_id=run.id,
                    recipient_id=recipient.id,
                    logical_identity=logical,
                    state=RecipientAttemptState.PENDING.value,
                    attempt_number=1,
                    p14_idempotency_key=downstream_key("workflow", logical),
                    p09_idempotency_key=downstream_key("message", logical),
                )
            )
        created = len(recipients)
        run.attempt_materialization_cursor += created
        total = int(
            (
                await self.session.execute(
                    select(func.count(CampaignRecipientRecord.id)).where(
                        CampaignRecipientRecord.organization_id == self.organization_id,
                        CampaignRecipientRecord.snapshot_id == run.audience_snapshot_id,
                        CampaignRecipientRecord.state.in_(
                            [RecipientState.ELIGIBLE.value, RecipientState.DEFERRED.value]
                        ),
                    )
                )
            ).scalar_one()
        )
        if run.attempt_materialization_cursor >= total:
            run.attempt_materialization_complete = True
            run.state = CampaignRunState.RUNNING.value
            await self.transition(
                "RUN",
                run.id,
                CampaignRunState.MATERIALIZING.value,
                run.state,
                "ATTEMPT_MATERIALIZATION_COMPLETED",
            )
        await self.session.flush()
        await self.session.refresh(run)
        return _run(run), created

    async def campaign_runs(self, campaign_id: uuid.UUID, *, limit: int) -> list[CampaignRun]:
        rows = (
            (
                await self.session.execute(
                    select(CampaignRunRecord)
                    .where(
                        CampaignRunRecord.organization_id == self.organization_id,
                        CampaignRunRecord.campaign_id == campaign_id,
                    )
                    .order_by(CampaignRunRecord.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_run(row) for row in rows]

    async def claim_recipient(
        self, run_id: uuid.UUID, owner_id: uuid.UUID
    ) -> RecipientClaim | None:
        run = await self.run_row(run_id, for_update=True)
        if run is None or run.state != CampaignRunState.RUNNING.value:
            return None
        campaign = await self.campaign_row(run.campaign_id, for_update=True)
        if campaign is None or campaign.state != CampaignState.RUNNING.value:
            return None
        now = await self.database_now()
        attempt = (
            await self.session.execute(
                select(CampaignRecipientAttemptRecord)
                .join(
                    CampaignRecipientRecord,
                    and_(
                        CampaignRecipientRecord.id == CampaignRecipientAttemptRecord.recipient_id,
                        CampaignRecipientRecord.organization_id
                        == CampaignRecipientAttemptRecord.organization_id,
                    ),
                )
                .where(
                    CampaignRecipientAttemptRecord.organization_id == self.organization_id,
                    CampaignRecipientAttemptRecord.campaign_run_id == run_id,
                    CampaignRecipientAttemptRecord.state == RecipientAttemptState.PENDING.value,
                    or_(
                        CampaignRecipientRecord.next_eligible_at.is_(None),
                        CampaignRecipientRecord.next_eligible_at <= now,
                    ),
                )
                .order_by(
                    CampaignRecipientAttemptRecord.created_at, CampaignRecipientAttemptRecord.id
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
        ).scalar_one_or_none()
        if attempt is None:
            return None
        token = uuid.uuid7()
        resolve_attempt_transition(
            RecipientAttemptState(attempt.state), RecipientAttemptState.CLAIMED
        )
        attempt.state = RecipientAttemptState.CLAIMED.value
        attempt.owner_id = owner_id
        attempt.claim_token = token
        attempt.claimed_at = now
        run.claimed_count += 1
        recipient = (
            await self.session.execute(
                select(CampaignRecipientRecord).where(
                    CampaignRecipientRecord.organization_id == self.organization_id,
                    CampaignRecipientRecord.id == attempt.recipient_id,
                )
            )
        ).scalar_one()
        await self.transition(
            "ATTEMPT",
            attempt.id,
            RecipientAttemptState.PENDING.value,
            attempt.state,
            "RECIPIENT_CLAIMED",
        )
        await self.session.flush()
        await self.session.refresh(run)
        await self.session.refresh(attempt)
        return RecipientClaim(
            run=_run(run),
            recipient=_recipient(recipient),
            attempt=_attempt(attempt),
            owner_id=owner_id,
            claim_token=token,
        )

    async def mark_workflow_started(
        self, claim: RecipientClaim, workflow_run_id: uuid.UUID
    ) -> RecipientAttempt:
        row = await self._owned_attempt(claim, RecipientAttemptState.CLAIMED)
        resolve_attempt_transition(
            RecipientAttemptState(row.state), RecipientAttemptState.WORKFLOW_RUNNING
        )
        row.state = RecipientAttemptState.WORKFLOW_RUNNING.value
        row.workflow_run_id = workflow_run_id
        await self.transition(
            "ATTEMPT",
            row.id,
            RecipientAttemptState.CLAIMED.value,
            row.state,
            "RECIPIENT_WORKFLOW_STARTED",
        )
        await self.session.flush()
        await self.session.refresh(row)
        return _attempt(row)

    async def mark_workflow_completed(self, claim: RecipientClaim) -> RecipientAttempt:
        row = await self._owned_attempt(claim, RecipientAttemptState.WORKFLOW_RUNNING)
        resolve_attempt_transition(
            RecipientAttemptState(row.state), RecipientAttemptState.READY_TO_SEND
        )
        row.state = RecipientAttemptState.READY_TO_SEND.value
        await self.transition(
            "ATTEMPT",
            row.id,
            RecipientAttemptState.WORKFLOW_RUNNING.value,
            row.state,
            "RECIPIENT_WORKFLOW_COMPLETED",
        )
        await self.session.flush()
        await self.session.refresh(row)
        return _attempt(row)

    async def owned_workflow_run_id(self, claim: RecipientClaim) -> uuid.UUID:
        row = await self._owned_attempt(claim, RecipientAttemptState.WORKFLOW_RUNNING)
        if row.workflow_run_id is None:
            raise CampaignInvalidStateError("recipient workflow has not started")
        return row.workflow_run_id

    async def authorize_permit(
        self, claim: RecipientClaim
    ) -> tuple[SendPermit | None, EligibilityReason | None]:
        run = await self.run_row(claim.run.id, for_update=True)
        if run is None or run.state != CampaignRunState.RUNNING.value:
            raise CampaignExecutionFencedError("campaign run no longer authorizes sends")
        campaign = await self.campaign_row(run.campaign_id, for_update=True)
        if campaign is None or campaign.state != CampaignState.RUNNING.value:
            raise CampaignExecutionFencedError("campaign no longer authorizes sends")
        attempt = await self._owned_attempt(claim, RecipientAttemptState.READY_TO_SEND)
        recipient = (
            await self.session.execute(
                select(CampaignRecipientRecord)
                .where(
                    CampaignRecipientRecord.organization_id == self.organization_id,
                    CampaignRecipientRecord.id == attempt.recipient_id,
                )
                .with_for_update()
            )
        ).scalar_one()
        revision = await self.revision_row(run.campaign_revision_id)
        if revision is None:
            raise CampaignExecutionFencedError("immutable campaign revision is absent")
        spec = CampaignDraft.model_validate(revision.specification)
        reason, next_at, consent_epoch, suppression_epoch, _ = await self.eligibility(
            campaign.id,
            recipient.customer_id,
            recipient.identity_id,
            recipient.conversation_id,
            recipient.channel,
            spec.quiet_hours,
            for_update=True,
        )
        if reason is not EligibilityReason.ELIGIBLE:
            recipient.state = (
                RecipientState.DEFERRED.value
                if reason is EligibilityReason.DEFERRED_QUIET_HOURS
                else RecipientState.SUPPRESSED.value
            )
            recipient.eligibility_reason = reason.value
            recipient.next_eligible_at = next_at
            recipient.evaluated_consent_epoch = consent_epoch
            recipient.evaluated_suppression_epoch = suppression_epoch
            attempt.error_code = reason.value
            previous_attempt_state = attempt.state
            if reason is not EligibilityReason.DEFERRED_QUIET_HOURS:
                resolve_attempt_transition(
                    RecipientAttemptState(attempt.state), RecipientAttemptState.SUPPRESSED
                )
                attempt.state = RecipientAttemptState.SUPPRESSED.value
                run.suppressed_count += 1
            await self.transition(
                "ATTEMPT",
                attempt.id,
                previous_attempt_state,
                attempt.state,
                reason.value,
            )
            await self.session.flush()
            return None, reason
        await self._reserve_throttle(run, recipient.channel, spec.throttle)
        existing = (
            await self.session.execute(
                select(CampaignSendPermitRecord).where(
                    CampaignSendPermitRecord.organization_id == self.organization_id,
                    CampaignSendPermitRecord.recipient_attempt_id == attempt.id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return _permit(existing), None
        now = await self.database_now()
        permit = CampaignSendPermitRecord(
            id=uuid.uuid7(),
            organization_id=self.organization_id,
            recipient_attempt_id=attempt.id,
            state=SendPermitState.AUTHORIZED.value,
            permit_token=uuid.uuid7(),
            consent_epoch=consent_epoch,
            suppression_epoch=suppression_epoch,
            p09_idempotency_key=attempt.p09_idempotency_key,
            authorized_at=now,
        )
        self.session.add(permit)
        resolve_attempt_transition(
            RecipientAttemptState(attempt.state), RecipientAttemptState.DISPATCH_AUTHORIZED
        )
        attempt.state = RecipientAttemptState.DISPATCH_AUTHORIZED.value
        attempt.error_code = None
        run.authorized_count += 1
        await self.transition("PERMIT", permit.id, None, permit.state, "SEND_AUTHORIZED")
        await self.transition(
            "ATTEMPT",
            attempt.id,
            RecipientAttemptState.READY_TO_SEND.value,
            attempt.state,
            "SEND_AUTHORIZED",
        )
        await self.session.flush()
        return _permit(permit), None

    async def dispatch_context(
        self, permit_id: uuid.UUID
    ) -> tuple[SendPermit, CampaignDraft, CampaignRecipient, RecipientAttempt]:
        permit = (
            await self.session.execute(
                select(CampaignSendPermitRecord).where(
                    CampaignSendPermitRecord.organization_id == self.organization_id,
                    CampaignSendPermitRecord.id == permit_id,
                )
            )
        ).scalar_one_or_none()
        if permit is None:
            raise CampaignExecutionFencedError("send permit is absent")
        attempt = (
            await self.session.execute(
                select(CampaignRecipientAttemptRecord).where(
                    CampaignRecipientAttemptRecord.organization_id == self.organization_id,
                    CampaignRecipientAttemptRecord.id == permit.recipient_attempt_id,
                )
            )
        ).scalar_one()
        recipient = (
            await self.session.execute(
                select(CampaignRecipientRecord).where(
                    CampaignRecipientRecord.organization_id == self.organization_id,
                    CampaignRecipientRecord.id == attempt.recipient_id,
                )
            )
        ).scalar_one()
        run = await self.run_row(attempt.campaign_run_id)
        if run is None:
            raise CampaignExecutionFencedError("campaign run is absent")
        revision = await self.revision_row(run.campaign_revision_id)
        if revision is None:
            raise CampaignExecutionFencedError("campaign revision is absent")
        return (
            _permit(permit),
            CampaignDraft.model_validate(revision.specification),
            _recipient(recipient),
            _attempt(attempt),
        )

    async def consume_permit(
        self, permit_id: uuid.UUID, permit_token: uuid.UUID, message_id: uuid.UUID
    ) -> SendPermit:
        permit = (
            await self.session.execute(
                select(CampaignSendPermitRecord)
                .where(
                    CampaignSendPermitRecord.organization_id == self.organization_id,
                    CampaignSendPermitRecord.id == permit_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if permit is None:
            raise CampaignExecutionFencedError("send permit is absent")
        if permit.state == SendPermitState.CONSUMED.value and permit.message_id == message_id:
            return _permit(permit)
        if permit.state != SendPermitState.AUTHORIZED.value or permit.permit_token != permit_token:
            raise CampaignExecutionFencedError("send permit authority is stale")
        attempt = (
            await self.session.execute(
                select(CampaignRecipientAttemptRecord)
                .where(
                    CampaignRecipientAttemptRecord.organization_id == self.organization_id,
                    CampaignRecipientAttemptRecord.id == permit.recipient_attempt_id,
                )
                .with_for_update()
            )
        ).scalar_one()
        if attempt.state != RecipientAttemptState.DISPATCH_AUTHORIZED.value:
            raise CampaignExecutionFencedError("recipient dispatch authority is stale")
        now = await self.database_now()
        permit.state = SendPermitState.CONSUMED.value
        permit.consumed_at = now
        permit.message_id = message_id
        attempt.state = RecipientAttemptState.DISPATCHED.value
        attempt.message_id = message_id
        run = await self.run_row(attempt.campaign_run_id, for_update=True)
        if run is not None:
            run.dispatched_count += 1
        await self.transition(
            "PERMIT", permit.id, SendPermitState.AUTHORIZED.value, permit.state, "SEND_CONSUMED"
        )
        await self.transition(
            "ATTEMPT",
            attempt.id,
            RecipientAttemptState.DISPATCH_AUTHORIZED.value,
            attempt.state,
            "RECIPIENT_DISPATCHED",
        )
        if run is not None and run.state == CampaignRunState.RUNNING.value:
            await self.session.flush()
            remaining = (
                await self.session.execute(
                    select(func.count(CampaignRecipientAttemptRecord.id)).where(
                        CampaignRecipientAttemptRecord.organization_id == self.organization_id,
                        CampaignRecipientAttemptRecord.campaign_run_id == run.id,
                        CampaignRecipientAttemptRecord.state.not_in(
                            [
                                RecipientAttemptState.DISPATCHED.value,
                                RecipientAttemptState.SUPPRESSED.value,
                                RecipientAttemptState.FAILED.value,
                                RecipientAttemptState.CANCELLED.value,
                            ]
                        ),
                    )
                )
            ).scalar_one()
            if remaining == 0:
                run.state = CampaignRunState.COMPLETED.value
                campaign = await self.campaign_row(run.campaign_id, for_update=True)
                if campaign is not None and campaign.state == CampaignState.RUNNING.value:
                    campaign.state = CampaignState.COMPLETED.value
                    await self.transition(
                        "RUN",
                        run.id,
                        CampaignRunState.RUNNING.value,
                        run.state,
                        "CAMPAIGN_RUN_COMPLETED",
                    )
                    await self.transition(
                        "CAMPAIGN",
                        campaign.id,
                        CampaignState.RUNNING.value,
                        campaign.state,
                        "CAMPAIGN_COMPLETED",
                    )
        await self.session.flush()
        return _permit(permit)

    async def begin_cancellation(self, campaign_id: uuid.UUID) -> Campaign:
        campaign = await self.campaign_row(campaign_id, for_update=True)
        if campaign is None:
            raise CampaignInvalidStateError("campaign does not exist")
        if campaign.state == CampaignState.CANCELLED.value:
            return _campaign(campaign)
        if campaign.state != CampaignState.CANCELLING.value:
            previous = CampaignState(campaign.state)
            require_campaign_transition(previous, CampaignState.CANCELLING)
            campaign.state = CampaignState.CANCELLING.value
            campaign.revision += 1
            await self.transition(
                "CAMPAIGN",
                campaign.id,
                previous.value,
                campaign.state,
                "CAMPAIGN_CANCELLATION_STARTED",
            )
        runs = (
            (
                await self.session.execute(
                    select(CampaignRunRecord)
                    .where(
                        CampaignRunRecord.organization_id == self.organization_id,
                        CampaignRunRecord.campaign_id == campaign.id,
                        CampaignRunRecord.state.in_(
                            [
                                CampaignRunState.PENDING_RELEASE.value,
                                CampaignRunState.MATERIALIZING.value,
                                CampaignRunState.RUNNING.value,
                                CampaignRunState.PAUSED.value,
                            ]
                        ),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for run in runs:
            previous_run_state = run.state
            run.state = CampaignRunState.CANCELLING.value
            run.cancellation_complete = False
            await self.transition(
                "RUN",
                run.id,
                previous_run_state,
                run.state,
                "CAMPAIGN_RUN_CANCELLATION_STARTED",
            )
        await self.session.flush()
        await self.session.refresh(campaign)
        return _campaign(campaign)

    async def cancel_attempt_batch(
        self, campaign_id: uuid.UUID, *, limit: int
    ) -> tuple[Campaign, int, bool]:
        if limit < 1:
            raise ValueError("cancellation batch limit must be positive")
        campaign = await self.campaign_row(campaign_id, for_update=True)
        if campaign is None:
            raise CampaignInvalidStateError("campaign does not exist")
        if campaign.state == CampaignState.CANCELLED.value:
            return _campaign(campaign), 0, True
        if campaign.state != CampaignState.CANCELLING.value:
            raise CampaignInvalidStateError("campaign cancellation is not active")
        attempts = (
            (
                await self.session.execute(
                    select(CampaignRecipientAttemptRecord)
                    .join(
                        CampaignRunRecord,
                        and_(
                            CampaignRunRecord.id == CampaignRecipientAttemptRecord.campaign_run_id,
                            CampaignRunRecord.organization_id
                            == CampaignRecipientAttemptRecord.organization_id,
                        ),
                    )
                    .where(
                        CampaignRecipientAttemptRecord.organization_id == self.organization_id,
                        CampaignRunRecord.campaign_id == campaign_id,
                        CampaignRunRecord.state == CampaignRunState.CANCELLING.value,
                        CampaignRecipientAttemptRecord.state.in_(
                            [
                                RecipientAttemptState.PENDING.value,
                                RecipientAttemptState.CLAIMED.value,
                                RecipientAttemptState.WORKFLOW_RUNNING.value,
                                RecipientAttemptState.READY_TO_SEND.value,
                            ]
                        ),
                    )
                    .order_by(
                        CampaignRecipientAttemptRecord.created_at,
                        CampaignRecipientAttemptRecord.id,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        processed_by_run: dict[uuid.UUID, int] = {}
        for attempt in attempts:
            previous_attempt_state = attempt.state
            resolve_attempt_transition(
                RecipientAttemptState(attempt.state), RecipientAttemptState.CANCELLED
            )
            attempt.state = RecipientAttemptState.CANCELLED.value
            attempt.error_code = EligibilityReason.CANCELLED.value
            processed_by_run[attempt.campaign_run_id] = (
                processed_by_run.get(attempt.campaign_run_id, 0) + 1
            )
            recipient = (
                await self.session.execute(
                    select(CampaignRecipientRecord)
                    .where(
                        CampaignRecipientRecord.organization_id == self.organization_id,
                        CampaignRecipientRecord.id == attempt.recipient_id,
                    )
                    .with_for_update()
                )
            ).scalar_one()
            if recipient.state in {RecipientState.ELIGIBLE.value, RecipientState.DEFERRED.value}:
                recipient.state = RecipientState.CANCELLED.value
                recipient.eligibility_reason = EligibilityReason.CANCELLED.value
            await self.transition(
                "ATTEMPT",
                attempt.id,
                previous_attempt_state,
                attempt.state,
                "CAMPAIGN_CANCELLED",
            )
        for run_id, processed in processed_by_run.items():
            await self.session.execute(
                update(CampaignRunRecord)
                .where(
                    CampaignRunRecord.organization_id == self.organization_id,
                    CampaignRunRecord.id == run_id,
                )
                .values(
                    cancellation_processed_count=(
                        CampaignRunRecord.cancellation_processed_count + processed
                    )
                )
            )
        await self.session.flush()
        remaining = int(
            (
                await self.session.execute(
                    select(func.count(CampaignRecipientAttemptRecord.id))
                    .join(
                        CampaignRunRecord,
                        and_(
                            CampaignRunRecord.id == CampaignRecipientAttemptRecord.campaign_run_id,
                            CampaignRunRecord.organization_id
                            == CampaignRecipientAttemptRecord.organization_id,
                        ),
                    )
                    .where(
                        CampaignRecipientAttemptRecord.organization_id == self.organization_id,
                        CampaignRunRecord.campaign_id == campaign_id,
                        CampaignRunRecord.state == CampaignRunState.CANCELLING.value,
                        CampaignRecipientAttemptRecord.state.in_(
                            [
                                RecipientAttemptState.PENDING.value,
                                RecipientAttemptState.CLAIMED.value,
                                RecipientAttemptState.WORKFLOW_RUNNING.value,
                                RecipientAttemptState.READY_TO_SEND.value,
                            ]
                        ),
                    )
                )
            ).scalar_one()
        )
        complete = remaining == 0
        if complete:
            runs = (
                (
                    await self.session.execute(
                        select(CampaignRunRecord)
                        .where(
                            CampaignRunRecord.organization_id == self.organization_id,
                            CampaignRunRecord.campaign_id == campaign_id,
                            CampaignRunRecord.state == CampaignRunState.CANCELLING.value,
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            for run in runs:
                run.state = CampaignRunState.CANCELLED.value
                run.cancellation_complete = True
                await self.transition(
                    "RUN",
                    run.id,
                    CampaignRunState.CANCELLING.value,
                    run.state,
                    "CAMPAIGN_RUN_CANCELLED",
                )
            campaign.state = CampaignState.CANCELLED.value
            await self.transition(
                "CAMPAIGN",
                campaign.id,
                CampaignState.CANCELLING.value,
                campaign.state,
                "CAMPAIGN_CANCELLED",
            )
        await self.session.flush()
        await self.session.refresh(campaign)
        return _campaign(campaign), len(attempts), complete

    async def transition_campaign_state(
        self, campaign_id: uuid.UUID, target: CampaignState, reason: str
    ) -> Campaign:
        row = await self.campaign_row(campaign_id, for_update=True)
        if row is None:
            raise CampaignInvalidStateError("campaign does not exist")
        previous = CampaignState(row.state)
        require_campaign_transition(previous, target)
        row.state = target.value
        row.revision += 1
        await self.transition("CAMPAIGN", row.id, previous.value, target.value, reason)
        if target in {CampaignState.PAUSED, CampaignState.FAILED}:
            run_target = {
                CampaignState.PAUSED: CampaignRunState.PAUSED,
                CampaignState.FAILED: CampaignRunState.FAILED,
            }[target]
            runs = (
                (
                    await self.session.execute(
                        select(CampaignRunRecord)
                        .where(
                            CampaignRunRecord.organization_id == self.organization_id,
                            CampaignRunRecord.campaign_id == row.id,
                            CampaignRunRecord.state.in_(
                                [
                                    CampaignRunState.PENDING_RELEASE.value,
                                    CampaignRunState.RUNNING.value,
                                ]
                            ),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            for run in runs:
                run.state = run_target.value
        await self.session.flush()
        await self.session.refresh(row)
        return _campaign(row)

    async def resume_campaign(self, campaign_id: uuid.UUID) -> Campaign:
        campaign = await self.transition_campaign_state(
            campaign_id, CampaignState.RUNNING, "CAMPAIGN_RESUMED"
        )
        await self.session.execute(
            update(CampaignRunRecord)
            .where(
                CampaignRunRecord.organization_id == self.organization_id,
                CampaignRunRecord.campaign_id == campaign_id,
                CampaignRunRecord.state == CampaignRunState.PAUSED.value,
            )
            .values(state=CampaignRunState.RUNNING.value)
        )
        return campaign

    async def set_preference(
        self,
        *,
        customer_id: uuid.UUID,
        identity_id: uuid.UUID,
        channel: str,
        consent_granted: bool,
        unsubscribed: bool,
        do_not_contact: bool,
        evidence_ref: str | None,
    ) -> int:
        row = (
            await self.session.execute(
                select(CampaignContactPreferenceRecord)
                .where(
                    CampaignContactPreferenceRecord.organization_id == self.organization_id,
                    CampaignContactPreferenceRecord.identity_id == identity_id,
                    CampaignContactPreferenceRecord.channel == channel,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            row = CampaignContactPreferenceRecord(
                id=uuid.uuid7(),
                organization_id=self.organization_id,
                customer_id=customer_id,
                identity_id=identity_id,
                channel=channel,
                consent_granted=consent_granted,
                unsubscribed=unsubscribed,
                do_not_contact=do_not_contact,
                consent_epoch=1,
                evidence_ref=evidence_ref,
            )
            self.session.add(row)
        else:
            if row.customer_id != customer_id:
                raise CampaignExecutionFencedError("identity/customer ownership changed")
            row.consent_granted = consent_granted
            row.unsubscribed = unsubscribed
            row.do_not_contact = do_not_contact
            row.evidence_ref = evidence_ref
            row.consent_epoch += 1
        await self.session.flush()
        return row.consent_epoch

    async def add_suppression(self, request: SuppressionRequest) -> int:
        epoch = await self._policy_epoch(request.channel.value, for_update=True)
        epoch.suppression_epoch += 1
        self.session.add(
            CampaignSuppressionRecord(
                id=uuid.uuid7(),
                organization_id=self.organization_id,
                channel=request.channel.value,
                scope=request.scope.value,
                campaign_id=request.campaign_id,
                customer_id=request.customer_id,
                identity_id=request.identity_id,
                reason_code=request.reason_code,
                active=True,
            )
        )
        await self.session.flush()
        return epoch.suppression_epoch

    async def eligibility(
        self,
        campaign_id: uuid.UUID,
        customer_id: uuid.UUID,
        identity_id: uuid.UUID,
        conversation_id: uuid.UUID,
        channel: str,
        quiet_hours: Any,
        *,
        for_update: bool = False,
    ) -> tuple[EligibilityReason, dt.datetime | None, int, int, str]:
        customer_query = select(CustomerRecord).where(
            CustomerRecord.organization_id == self.organization_id,
            CustomerRecord.id == customer_id,
        )
        identity_query = select(CustomerIdentityRecord).where(
            CustomerIdentityRecord.organization_id == self.organization_id,
            CustomerIdentityRecord.id == identity_id,
            CustomerIdentityRecord.customer_id == customer_id,
        )
        conversation_query = select(ConversationRecord).where(
            ConversationRecord.organization_id == self.organization_id,
            ConversationRecord.id == conversation_id,
            ConversationRecord.customer_id == customer_id,
        )
        preference_query = select(CampaignContactPreferenceRecord).where(
            CampaignContactPreferenceRecord.organization_id == self.organization_id,
            CampaignContactPreferenceRecord.identity_id == identity_id,
            CampaignContactPreferenceRecord.channel == channel,
        )
        if for_update:
            customer_query = customer_query.with_for_update()
            identity_query = identity_query.with_for_update()
            conversation_query = conversation_query.with_for_update()
            preference_query = preference_query.with_for_update()
        customer = (await self.session.execute(customer_query)).scalar_one_or_none()
        identity = (await self.session.execute(identity_query)).scalar_one_or_none()
        conversation = (await self.session.execute(conversation_query)).scalar_one_or_none()
        epoch = await self._policy_epoch(channel, for_update=for_update)
        preference = (await self.session.execute(preference_query)).scalar_one_or_none()
        consent_epoch = 0 if preference is None else preference.consent_epoch
        destination = "invalid" if identity is None else identity.normalized_value
        if customer is None or customer.status != "ACTIVE":
            return (
                EligibilityReason.CUSTOMER_INACTIVE,
                None,
                consent_epoch,
                epoch.suppression_epoch,
                destination,
            )
        if identity is None or identity.status != "ACTIVE":
            return (
                EligibilityReason.IDENTITY_INACTIVE,
                None,
                consent_epoch,
                epoch.suppression_epoch,
                destination,
            )
        expected_identity = "EMAIL" if channel == "EMAIL" else "PHONE"
        if identity.identity_type != expected_identity:
            return (
                EligibilityReason.INVALID_DESTINATION,
                None,
                consent_epoch,
                epoch.suppression_epoch,
                destination,
            )
        if (
            conversation is None
            or conversation.status == "CLOSED"
            or conversation.channel.upper() != channel
        ):
            return (
                EligibilityReason.NO_CHANNEL,
                None,
                consent_epoch,
                epoch.suppression_epoch,
                destination,
            )
        if preference is not None and preference.unsubscribed:
            return (
                EligibilityReason.UNSUBSCRIBED,
                None,
                consent_epoch,
                epoch.suppression_epoch,
                destination,
            )
        if preference is not None and preference.do_not_contact:
            return (
                EligibilityReason.DO_NOT_CONTACT,
                None,
                consent_epoch,
                epoch.suppression_epoch,
                destination,
            )
        if preference is None or not preference.consent_granted:
            return (
                EligibilityReason.NO_CONSENT,
                None,
                consent_epoch,
                epoch.suppression_epoch,
                destination,
            )
        suppression_query = (
            select(CampaignSuppressionRecord)
            .where(
                CampaignSuppressionRecord.organization_id == self.organization_id,
                CampaignSuppressionRecord.channel == channel,
                CampaignSuppressionRecord.active.is_(True),
                or_(
                    CampaignSuppressionRecord.scope == "GLOBAL",
                    and_(
                        CampaignSuppressionRecord.scope == "CAMPAIGN",
                        CampaignSuppressionRecord.campaign_id == campaign_id,
                    ),
                    and_(
                        CampaignSuppressionRecord.scope == "CUSTOMER",
                        CampaignSuppressionRecord.customer_id == customer_id,
                    ),
                    and_(
                        CampaignSuppressionRecord.scope == "IDENTITY",
                        CampaignSuppressionRecord.identity_id == identity_id,
                    ),
                ),
            )
            .limit(1)
        )
        if for_update:
            suppression_query = suppression_query.with_for_update()
        suppression = (await self.session.execute(suppression_query)).scalar_one_or_none()
        if suppression is not None:
            reason = (
                EligibilityReason.SUPPRESSED_CAMPAIGN
                if suppression.scope == "CAMPAIGN"
                else EligibilityReason.SUPPRESSED_GLOBAL
            )
            return reason, None, consent_epoch, epoch.suppression_epoch, destination
        now = await self.database_now()
        next_at = self._quiet_hours_next(now, quiet_hours)
        if next_at is not None:
            return (
                EligibilityReason.DEFERRED_QUIET_HOURS,
                next_at,
                consent_epoch,
                epoch.suppression_epoch,
                destination,
            )
        return EligibilityReason.ELIGIBLE, None, consent_epoch, epoch.suppression_epoch, destination

    async def _policy_epoch(
        self, channel: str, *, for_update: bool = False
    ) -> CampaignPolicyEpochRecord:
        query = select(CampaignPolicyEpochRecord).where(
            CampaignPolicyEpochRecord.organization_id == self.organization_id,
            CampaignPolicyEpochRecord.channel == channel,
        )
        if for_update:
            query = query.with_for_update()
        row = (await self.session.execute(query)).scalar_one_or_none()
        if row is None:
            row = CampaignPolicyEpochRecord(
                id=uuid.uuid7(),
                organization_id=self.organization_id,
                channel=channel,
                suppression_epoch=1,
            )
            self.session.add(row)
            await self.session.flush()
        return row

    @staticmethod
    def _quiet_hours_next(now: dt.datetime, policy: Any) -> dt.datetime | None:
        if not policy.enabled:
            return None
        zone = ZoneInfo(policy.timezone)
        local = now.astimezone(zone)
        current = local.timetz().replace(tzinfo=None)
        start, end = policy.start_local, policy.end_local
        inside = (start < end and start <= current < end) or (
            start >= end and (current >= start or current < end)
        )
        if not inside:
            return None
        end_date = local.date() + dt.timedelta(days=1 if start >= end and current >= start else 0)
        return dt.datetime.combine(end_date, end, zone).astimezone(dt.UTC)

    async def _reserve_throttle(
        self, run: CampaignRunRecord, channel: str, policy: ThrottlePolicy
    ) -> None:
        now = await self.database_now()
        window = now.replace(second=0, microsecond=0)
        await self.session.execute(
            pg_insert(CampaignThrottleWindowRecord)
            .values(
                id=uuid.uuid7(),
                organization_id=self.organization_id,
                campaign_run_id=run.id,
                channel=channel,
                window_start=window,
                reserved_count=0,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    CampaignThrottleWindowRecord.organization_id,
                    CampaignThrottleWindowRecord.campaign_run_id,
                    CampaignThrottleWindowRecord.channel,
                    CampaignThrottleWindowRecord.window_start,
                ]
            )
        )
        run_window = (
            await self.session.execute(
                select(CampaignThrottleWindowRecord)
                .where(
                    CampaignThrottleWindowRecord.organization_id == self.organization_id,
                    CampaignThrottleWindowRecord.campaign_run_id == run.id,
                    CampaignThrottleWindowRecord.channel == channel,
                    CampaignThrottleWindowRecord.window_start == window,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if run_window is None:
            raise CampaignExecutionFencedError("campaign throttle window could not be locked")
        await self.session.execute(
            pg_insert(CampaignOrganizationThrottleWindowRecord)
            .values(
                id=uuid.uuid7(),
                organization_id=self.organization_id,
                channel=channel,
                window_start=window,
                reserved_count=0,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    CampaignOrganizationThrottleWindowRecord.organization_id,
                    CampaignOrganizationThrottleWindowRecord.channel,
                    CampaignOrganizationThrottleWindowRecord.window_start,
                ]
            )
        )
        organization_window = (
            await self.session.execute(
                select(CampaignOrganizationThrottleWindowRecord)
                .where(
                    CampaignOrganizationThrottleWindowRecord.organization_id
                    == self.organization_id,
                    CampaignOrganizationThrottleWindowRecord.channel == channel,
                    CampaignOrganizationThrottleWindowRecord.window_start == window,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if organization_window is None:
            raise CampaignExecutionFencedError("Organization throttle window could not be locked")
        if run.authorized_count >= policy.campaign_limit:
            raise CampaignInvalidStateError("campaign total logical-send cap is exhausted")
        if run_window.reserved_count >= policy.messages_per_minute:
            raise CampaignInvalidStateError("campaign throttle window is exhausted")
        if organization_window.reserved_count >= policy.organization_messages_per_minute:
            raise CampaignInvalidStateError("Organization throttle window is exhausted")
        run_window.reserved_count += 1
        organization_window.reserved_count += 1

    async def _owned_attempt(
        self, claim: RecipientClaim, state: RecipientAttemptState
    ) -> CampaignRecipientAttemptRecord:
        row = (
            await self.session.execute(
                select(CampaignRecipientAttemptRecord)
                .where(
                    CampaignRecipientAttemptRecord.organization_id == self.organization_id,
                    CampaignRecipientAttemptRecord.id == claim.attempt.id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            row is None
            or row.state != state.value
            or row.owner_id != claim.owner_id
            or row.claim_token != claim.claim_token
        ):
            raise CampaignExecutionFencedError("recipient execution owner or token is stale")
        return row

    async def transition(
        self,
        entity_type: str,
        entity_id: uuid.UUID,
        from_state: str | None,
        to_state: str,
        reason_code: str,
        *,
        source: str = "SYSTEM",
        correlation_id: str | None = None,
    ) -> None:
        self.session.add(
            CampaignTransitionHistoryRecord(
                id=uuid.uuid7(),
                organization_id=self.organization_id,
                entity_type=entity_type,
                entity_id=entity_id,
                from_state=from_state,
                to_state=to_state,
                reason_code=reason_code,
                source=source,
                correlation_id=correlation_id,
            )
        )

    async def transitions(self, campaign_id: uuid.UUID, *, limit: int) -> list[CampaignTransition]:
        rows = (
            (
                await self.session.execute(
                    select(CampaignTransitionHistoryRecord)
                    .where(
                        CampaignTransitionHistoryRecord.organization_id == self.organization_id,
                        CampaignTransitionHistoryRecord.entity_id == campaign_id,
                    )
                    .order_by(
                        CampaignTransitionHistoryRecord.created_at,
                        CampaignTransitionHistoryRecord.id,
                    )
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [
            CampaignTransition(
                id=row.id,
                organization_id=row.organization_id,
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                from_state=row.from_state,
                to_state=row.to_state,
                reason_code=row.reason_code,
                correlation_id=row.correlation_id,
                created_at=row.created_at,
            )
            for row in rows
        ]
