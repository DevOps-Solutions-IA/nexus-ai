"""Durable Campaign orchestration across P15, P14 and P09 certified boundaries."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError

from nexus_ai.campaigns import events as campaign_events  # noqa: F401 - register payloads
from nexus_ai.campaigns.entities import (
    Campaign,
    CampaignRecipient,
    CampaignRun,
    CampaignTransition,
    ContactPreferenceRequest,
    CreateCampaignRequest,
    RecipientAttempt,
    RecipientClaim,
    ScheduleCampaignRequest,
    SendPermit,
    SuppressionRequest,
    UpdateCampaignRequest,
)
from nexus_ai.campaigns.errors import (
    CampaignConflictError,
    CampaignDispatchFailedError,
    CampaignExecutionFencedError,
    CampaignInvalidStateError,
    CampaignNotFoundError,
    CampaignRunNotFoundError,
)
from nexus_ai.campaigns.state_machine import CampaignState, SendPermitState
from nexus_ai.core.context import current_context
from nexus_ai.core.errors import NxsError
from nexus_ai.domain.campaigns.repository import CampaignRepository
from nexus_ai.domain.customers.models import CustomerIdentityRecord
from nexus_ai.domain.messaging.repository import MessagingAccountRepository
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.publisher import EventPublisher
from nexus_ai.infrastructure.database import Database
from nexus_ai.messaging.entities import OutboundAddressInput, SendMessageRequest
from nexus_ai.messaging.service import MessagingService
from nexus_ai.scheduler.entities import CreateScheduleRequest
from nexus_ai.scheduler.errors import ScheduleConflictError
from nexus_ai.scheduler.service import SchedulerService
from nexus_ai.scheduler.state_machine import ScheduleState
from nexus_ai.workflows.entities import StartWorkflowRunRequest
from nexus_ai.workflows.service import WorkflowService
from nexus_ai.workflows.state_machine import WorkflowRunState

MATERIALIZATION_BATCH = 100
CANCELLATION_BATCH = 100


class CampaignService:
    def __init__(
        self,
        database: Database,
        publisher: EventPublisher,
        scheduler: SchedulerService,
        workflows: WorkflowService,
        messaging: MessagingService,
        *,
        service_name: str,
    ) -> None:
        self._db = database
        self._publisher = publisher
        self._scheduler = scheduler
        self._workflows = workflows
        self._messaging = messaging
        self._service_name = service_name

    async def create_campaign(
        self, organization_id: uuid.UUID, request: CreateCampaignRequest
    ) -> Campaign:
        await self._validate_draft(organization_id, request.draft)
        try:
            async with self._db.tenant_transaction(organization_id) as tenant:
                campaign = await CampaignRepository(tenant).create_campaign(
                    request.campaign_key, request.name, request.draft
                )
                await self._campaign_event(
                    tenant.session, organization_id, "campaign.created", campaign
                )
                return campaign
        except IntegrityError as exc:
            raise CampaignConflictError("campaign_key already exists in this Organization") from exc

    async def get_campaign(self, organization_id: uuid.UUID, campaign_id: uuid.UUID) -> Campaign:
        async with self._db.tenant_transaction(organization_id) as tenant:
            campaign = await CampaignRepository(tenant).campaign(campaign_id)
            if campaign is None:
                raise CampaignNotFoundError()
            return campaign

    async def list_campaigns(
        self, organization_id: uuid.UUID, *, limit: int, offset: int
    ) -> list[Campaign]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await CampaignRepository(tenant).list_campaigns(limit=limit, offset=offset)

    async def update_campaign(
        self,
        organization_id: uuid.UUID,
        campaign_id: uuid.UUID,
        request: UpdateCampaignRequest,
    ) -> Campaign:
        if request.draft is not None:
            await self._validate_draft(organization_id, request.draft)
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = CampaignRepository(tenant)
            row = await repo.campaign_row(campaign_id, for_update=True)
            if row is None:
                raise CampaignNotFoundError()
            if row.revision != request.expected_revision:
                raise CampaignConflictError("campaign revision changed; reload before editing")
            return await repo.update_campaign(row, name=request.name, draft=request.draft)

    async def prepare_campaign(
        self, organization_id: uuid.UUID, campaign_id: uuid.UUID
    ) -> Campaign:
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = CampaignRepository(tenant)
            row = await repo.campaign_row(campaign_id, for_update=True)
            if row is None:
                raise CampaignNotFoundError()
            if row.state == CampaignState.DRAFT.value:
                revision, snapshot = await repo.begin_preparation(row)
            elif row.state == CampaignState.PREPARING.value:
                revision, snapshot = await repo.active_preparation(row.id, row.revision)
            else:
                raise CampaignInvalidStateError("campaign is not available for preparation")
        while True:
            async with self._db.tenant_transaction(organization_id) as tenant:
                snapshot = await CampaignRepository(tenant).materialize_batch(
                    revision.id, limit=MATERIALIZATION_BATCH
                )
            if snapshot.cursor >= len(revision.specification.audience):
                break
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = CampaignRepository(tenant)
            campaign = await repo.seal_preparation(campaign_id, revision.id)
            await self._campaign_event(
                tenant.session,
                organization_id,
                "campaign.prepared",
                campaign,
                count=snapshot.resolved_count,
            )
            return campaign

    async def schedule_campaign(
        self,
        organization_id: uuid.UUID,
        campaign_id: uuid.UUID,
        request: ScheduleCampaignRequest,
    ) -> Campaign:
        campaign = await self.get_campaign(organization_id, campaign_id)
        if campaign.prepared_revision_id is None or campaign.state not in {
            CampaignState.READY,
            CampaignState.SCHEDULED,
        }:
            raise CampaignInvalidStateError("campaign must be READY or SCHEDULED")
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = CampaignRepository(tenant)
            row = await repo.campaign_row(campaign_id, for_update=True)
            if row is None or row.prepared_revision_id is None:
                raise CampaignInvalidStateError("campaign revision is absent")
            run = await repo.create_run(
                row,
                idempotency_key=f"campaign:scheduled:{campaign_id}:{row.prepared_revision_id}",
            )
            revision = await repo.revision_row(run.campaign_revision_id)
            if revision is None:
                raise CampaignInvalidStateError("campaign revision is absent")
        schedule_key = f"campaign.{campaign_id.hex}.r{revision.revision_number}"
        schedule_input = {
            "campaign_id": str(campaign_id),
            "campaign_revision_id": str(revision.id),
            "campaign_run_id": str(run.id),
        }
        schedule_request = CreateScheduleRequest(
            schedule_key=schedule_key,
            workflow_version_id=revision.release_workflow_version_id,
            schedule_type=request.schedule_type,
            timezone=request.timezone,
            start_at=request.start_at,
            end_at=request.end_at,
            recurrence=request.recurrence,
            input=schedule_input,
            misfire_policy=request.misfire_policy,
            max_catch_up=request.max_catch_up,
            correlation_id=self._correlation_id(),
        )
        try:
            schedule = await self._scheduler.create_schedule(organization_id, schedule_request)
        except ScheduleConflictError:
            schedule = await self._scheduler.get_schedule_by_key(organization_id, schedule_key)
        if (
            schedule.workflow_version_id != revision.release_workflow_version_id
            or schedule.input != schedule_input
            or schedule.schedule_type is not request.schedule_type
            or schedule.timezone != request.timezone
            or schedule.start_at != request.start_at
            or schedule.end_at != request.end_at
            or schedule.recurrence != request.recurrence
            or schedule.misfire_policy is not request.misfire_policy
            or schedule.max_catch_up != request.max_catch_up
        ):
            raise CampaignExecutionFencedError(
                "existing campaign schedule does not match the immutable release binding"
            )
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = CampaignRepository(tenant)
            await repo.bind_schedule(run.id, schedule.id)
            result = await repo.mark_campaign_scheduled(campaign_id)
            await self._campaign_event(
                tenant.session, organization_id, "campaign.scheduled", result
            )
        if schedule.state is ScheduleState.DRAFT:
            await self._scheduler.activate_schedule(organization_id, schedule.id)
        elif schedule.state is not ScheduleState.ACTIVE:
            raise CampaignExecutionFencedError("campaign schedule is not active")
        return result

    async def start_campaign(
        self,
        organization_id: uuid.UUID,
        campaign_id: uuid.UUID,
        *,
        idempotency_key: str | None = None,
    ) -> CampaignRun:
        campaign = await self.get_campaign(organization_id, campaign_id)
        key = idempotency_key or f"campaign:run:{campaign.id}:{campaign.revision}"
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = CampaignRepository(tenant)
            row = await repo.campaign_row(campaign_id, for_update=True)
            if row is None:
                raise CampaignNotFoundError()
            run = await repo.create_run(row, idempotency_key=key)
            revision = await repo.revision_row(run.campaign_revision_id)
            if revision is None:
                raise CampaignInvalidStateError("campaign revision is absent")
        release = await self._workflows.start_run(
            organization_id,
            StartWorkflowRunRequest(
                workflow_version_id=revision.release_workflow_version_id,
                input={"campaign_id": str(campaign_id), "campaign_run_id": str(run.id)},
                idempotency_key=f"campaign:release:{run.id}",
                correlation_id=self._correlation_id(),
            ),
        )
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await CampaignRepository(tenant).set_release_run(run.id, release.id)

    async def confirm_release(
        self, organization_id: uuid.UUID, campaign_run_id: uuid.UUID
    ) -> CampaignRun:
        async with self._db.tenant_transaction(organization_id) as tenant:
            row = await CampaignRepository(tenant).run_row(campaign_run_id)
            if row is None:
                raise CampaignRunNotFoundError()
            release_id = row.release_workflow_run_id
        if release_id is None:
            raise CampaignInvalidStateError("release workflow has not started")
        release = await self._workflows.get_run(organization_id, release_id)
        if release.state is not WorkflowRunState.COMPLETED:
            raise CampaignInvalidStateError("release workflow is not complete")
        async with self._db.tenant_transaction(organization_id) as tenant:
            run = await CampaignRepository(tenant).begin_run_activation(campaign_run_id)
        if not run.attempt_materialization_complete:
            async with self._db.tenant_transaction(organization_id) as tenant:
                run, _ = await CampaignRepository(tenant).materialize_attempt_batch(
                    campaign_run_id, limit=MATERIALIZATION_BATCH
                )
        if not run.attempt_materialization_complete:
            return run
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = CampaignRepository(tenant)
            campaign = await repo.campaign(run.campaign_id)
            if campaign is None:
                raise CampaignNotFoundError()
            await self._campaign_event(
                tenant.session, organization_id, "campaign.started", campaign
            )
            return run

    async def claim_recipient(
        self, organization_id: uuid.UUID, campaign_run_id: uuid.UUID, owner_id: uuid.UUID
    ) -> RecipientClaim | None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            claim = await CampaignRepository(tenant).claim_recipient(campaign_run_id, owner_id)
            if claim is not None:
                await self._recipient_event(
                    tenant.session,
                    organization_id,
                    "campaign.recipient.claimed",
                    claim,
                )
            return claim

    async def start_recipient_workflow(
        self, organization_id: uuid.UUID, claim: RecipientClaim
    ) -> RecipientAttempt:
        async with self._db.tenant_transaction(organization_id) as tenant:
            revision = await CampaignRepository(tenant).revision_row(claim.run.campaign_revision_id)
            if revision is None:
                raise CampaignExecutionFencedError("campaign revision is absent")
        workflow = await self._workflows.start_run(
            organization_id,
            StartWorkflowRunRequest(
                workflow_version_id=revision.recipient_workflow_version_id,
                input={
                    "campaign_id": str(claim.run.campaign_id),
                    "campaign_run_id": str(claim.run.id),
                    "recipient_id": str(claim.recipient.id),
                },
                idempotency_key=claim.attempt.p14_idempotency_key,
                correlation_id=self._correlation_id(),
            ),
        )
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = CampaignRepository(tenant)
            attempt = await repo.mark_workflow_started(claim, workflow.id)
            current_claim = claim.model_copy(update={"attempt": attempt})
            await self._recipient_event(
                tenant.session,
                organization_id,
                "campaign.recipient.workflow_started",
                current_claim,
            )
            return attempt

    async def confirm_recipient_workflow(
        self, organization_id: uuid.UUID, claim: RecipientClaim
    ) -> RecipientAttempt:
        async with self._db.tenant_transaction(organization_id) as tenant:
            workflow_run_id = await CampaignRepository(tenant).owned_workflow_run_id(claim)
        workflow = await self._workflows.get_run(organization_id, workflow_run_id)
        if workflow.state is not WorkflowRunState.COMPLETED:
            raise CampaignInvalidStateError("recipient workflow is not complete")
        if workflow.output is not None and len(str(workflow.output)) > 16_384:
            raise CampaignInvalidStateError("recipient workflow output is not bounded")
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await CampaignRepository(tenant).mark_workflow_completed(claim)

    async def authorize_send(self, organization_id: uuid.UUID, claim: RecipientClaim) -> SendPermit:
        async with self._db.tenant_transaction(organization_id) as tenant:
            permit, denial = await CampaignRepository(tenant).authorize_permit(claim)
        if denial is not None:
            raise CampaignInvalidStateError(f"recipient final authorization denied: {denial.value}")
        if permit is None:
            raise CampaignExecutionFencedError("send authorization produced no durable decision")
        return permit

    async def dispatch_send(self, organization_id: uuid.UUID, permit_id: uuid.UUID) -> SendPermit:
        async with self._db.tenant_transaction(organization_id) as tenant:
            permit, spec, recipient, attempt = await CampaignRepository(tenant).dispatch_context(
                permit_id
            )
            identity = await tenant.session.get(
                CustomerIdentityRecord,
                recipient.identity_id,
            )
        if permit.state is SendPermitState.CONSUMED:
            return permit
        if permit.state is not SendPermitState.AUTHORIZED or identity is None:
            raise CampaignExecutionFencedError("send permit is not authorized")
        if (
            hashlib.sha256(identity.normalized_value.encode()).hexdigest()
            != recipient.destination_fingerprint
        ):
            raise CampaignExecutionFencedError("recipient destination changed after snapshot")
        try:
            message = await self._messaging.send(
                organization_id,
                None,
                SendMessageRequest(
                    account_id=spec.account_id,
                    conversation_id=recipient.conversation_id,
                    to=(OutboundAddressInput(value=identity.normalized_value),),
                    content=spec.content,
                    subject=spec.subject,
                    idempotency_key=permit.p09_idempotency_key,
                    correlation_id=self._correlation_id(),
                ),
            )
        except NxsError as exc:
            raise CampaignDispatchFailedError(
                "P09 rejected the permit-authorized campaign send",
                extensions={"upstream_code": exc.code},
            ) from exc
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = CampaignRepository(tenant)
            consumed = await repo.consume_permit(permit.id, permit.permit_token, message.id)
            current_run = await self._get_run_in_repo(repo, attempt.campaign_run_id)
            claim = RecipientClaim(
                run=current_run,
                recipient=recipient,
                attempt=attempt,
                owner_id=attempt.owner_id or uuid.UUID(int=0),
                claim_token=attempt.claim_token or uuid.UUID(int=0),
            )
            await self._recipient_event(
                tenant.session,
                organization_id,
                "campaign.recipient.dispatched",
                claim,
            )
            if current_run.state.value == "COMPLETED":
                campaign = await repo.campaign(current_run.campaign_id)
                if campaign is not None:
                    await self._campaign_event(
                        tenant.session,
                        organization_id,
                        "campaign.completed",
                        campaign,
                    )
            return consumed

    async def set_contact_preference(
        self, organization_id: uuid.UUID, request: ContactPreferenceRequest
    ) -> int:
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await CampaignRepository(tenant).set_preference(
                customer_id=request.customer_id,
                identity_id=request.identity_id,
                channel=request.channel.value,
                consent_granted=request.consent_granted,
                unsubscribed=request.unsubscribed,
                do_not_contact=request.do_not_contact,
                evidence_ref=request.evidence_ref,
            )

    async def add_suppression(self, organization_id: uuid.UUID, request: SuppressionRequest) -> int:
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await CampaignRepository(tenant).add_suppression(request)

    async def pause_campaign(self, organization_id: uuid.UUID, campaign_id: uuid.UUID) -> Campaign:
        return await self._transition(
            organization_id, campaign_id, CampaignState.PAUSED, "CAMPAIGN_PAUSED", "campaign.paused"
        )

    async def resume_campaign(self, organization_id: uuid.UUID, campaign_id: uuid.UUID) -> Campaign:
        async with self._db.tenant_transaction(organization_id) as tenant:
            campaign = await CampaignRepository(tenant).resume_campaign(campaign_id)
            await self._campaign_event(
                tenant.session, organization_id, "campaign.resumed", campaign
            )
            return campaign

    async def cancel_campaign(self, organization_id: uuid.UUID, campaign_id: uuid.UUID) -> Campaign:
        async with self._db.tenant_transaction(organization_id) as tenant:
            campaign = await CampaignRepository(tenant).begin_cancellation(campaign_id)
        if campaign.state is not CampaignState.CANCELLED:
            async with self._db.tenant_transaction(organization_id) as tenant:
                campaign, _, _ = await CampaignRepository(tenant).cancel_attempt_batch(
                    campaign_id, limit=CANCELLATION_BATCH
                )
        if campaign.state is not CampaignState.CANCELLED:
            return campaign
        async with self._db.tenant_transaction(organization_id) as tenant:
            await self._campaign_event(
                tenant.session, organization_id, "campaign.cancelled", campaign
            )
        return campaign

    async def _transition(
        self,
        organization_id: uuid.UUID,
        campaign_id: uuid.UUID,
        target: CampaignState,
        reason: str,
        event_type: str,
    ) -> Campaign:
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = CampaignRepository(tenant)
            if await repo.campaign(campaign_id) is None:
                raise CampaignNotFoundError()
            campaign = await repo.transition_campaign_state(campaign_id, target, reason)
            await self._campaign_event(tenant.session, organization_id, event_type, campaign)
            return campaign

    async def audience(
        self, organization_id: uuid.UUID, campaign_id: uuid.UUID, *, limit: int, offset: int
    ) -> list[CampaignRecipient]:
        await self.get_campaign(organization_id, campaign_id)
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await CampaignRepository(tenant).audience(
                campaign_id, limit=limit, offset=offset
            )

    async def runs(
        self, organization_id: uuid.UUID, campaign_id: uuid.UUID, *, limit: int
    ) -> list[CampaignRun]:
        await self.get_campaign(organization_id, campaign_id)
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await CampaignRepository(tenant).campaign_runs(campaign_id, limit=limit)

    async def transitions(
        self, organization_id: uuid.UUID, campaign_id: uuid.UUID, *, limit: int
    ) -> list[CampaignTransition]:
        await self.get_campaign(organization_id, campaign_id)
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await CampaignRepository(tenant).transitions(campaign_id, limit=limit)

    async def _validate_draft(self, organization_id: uuid.UUID, draft: Any) -> None:
        await self._workflows.get_version(organization_id, draft.release_workflow_version_id)
        await self._workflows.get_version(organization_id, draft.recipient_workflow_version_id)
        async with self._db.tenant_transaction(organization_id) as tenant:
            account = await MessagingAccountRepository(tenant).by_id(draft.account_id)
            if account is None or account.channel is not draft.channel:
                raise CampaignInvalidStateError("messaging account is absent or channel-mismatched")

    @staticmethod
    async def _get_run_in_repo(repo: CampaignRepository, run_id: uuid.UUID) -> CampaignRun:
        row = await repo.run_row(run_id)
        if row is None:
            raise CampaignRunNotFoundError()
        from nexus_ai.domain.campaigns.repository import _run

        return _run(row)

    async def _campaign_event(
        self,
        session: Any,
        organization_id: uuid.UUID,
        event_type: str,
        campaign: Campaign,
        *,
        count: int | None = None,
    ) -> None:
        await self._publisher.enqueue(
            session,
            EventEnvelope.create(
                event_type=event_type,
                event_version=1,
                aggregate_type="campaign",
                aggregate_id=str(campaign.id),
                producer=self._service_name,
                organization_id=organization_id,
                correlation_id=self._correlation_id(),
                payload={
                    "campaign_id": str(campaign.id),
                    "state": campaign.state.value,
                    "reason_code": None,
                    "count": count,
                },
            ),
        )

    async def _recipient_event(
        self,
        session: Any,
        organization_id: uuid.UUID,
        event_type: str,
        claim: RecipientClaim,
    ) -> None:
        await self._publisher.enqueue(
            session,
            EventEnvelope.create(
                event_type=event_type,
                event_version=1,
                aggregate_type="campaign_recipient",
                aggregate_id=str(claim.recipient.id),
                producer=self._service_name,
                organization_id=organization_id,
                correlation_id=self._correlation_id(),
                payload={
                    "campaign_id": str(claim.run.campaign_id),
                    "campaign_run_id": str(claim.run.id),
                    "recipient_id": str(claim.recipient.id),
                    "state": claim.attempt.state.value,
                    "reason_code": claim.attempt.error_code,
                    "count": None,
                },
            ),
        )

    @staticmethod
    def _correlation_id() -> str | None:
        context = current_context()
        return None if context is None else context.correlation_id
