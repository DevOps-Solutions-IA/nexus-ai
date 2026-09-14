"""NXS-P16 corrective #1 execution correctness over real PostgreSQL."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from nexus_ai.campaigns.entities import (
    AudienceMemberInput,
    CampaignDraft,
    ContactPreferenceRequest,
    CreateCampaignRequest,
    ScheduleCampaignRequest,
    ThrottlePolicy,
)
from nexus_ai.campaigns.errors import CampaignExecutionFencedError, CampaignInvalidStateError
from nexus_ai.campaigns.state_machine import CampaignRunState, CampaignState
from nexus_ai.domain.campaigns.models import (
    CampaignOrganizationThrottleWindowRecord,
    CampaignRecipientAttemptRecord,
    CampaignRunRecord,
    CampaignThrottleWindowRecord,
    CampaignTransitionHistoryRecord,
)
from nexus_ai.domain.campaigns.repository import CampaignRepository
from nexus_ai.domain.scheduler.models import SchedulerTransitionHistoryRecord
from nexus_ai.messaging.entities import MessageChannel, MessageContent
from nexus_ai.scheduler.entities import (
    CreateScheduleRequest,
    MisfirePolicy,
    RecurrenceFrequency,
    RecurrenceSpec,
    ScheduleType,
)
from tests.integration.test_campaign_service import (
    campaign_account,
    campaign_principal,
    campaign_recipient,
    campaign_workflow,
    prepared_campaign,
    ready_claim,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _prepared_members(
    stack: Any,
    organization_id: uuid.UUID,
    *,
    count: int,
    throttle: ThrottlePolicy,
    suffix_base: int = 200,
) -> Any:
    release = await campaign_workflow(stack, organization_id, f"release-{uuid.uuid4().hex[:6]}")
    recipient_workflow = await campaign_workflow(
        stack, organization_id, f"recipient-{uuid.uuid4().hex[:6]}"
    )
    account = await campaign_account(stack, organization_id)
    members: list[AudienceMemberInput] = []
    for index in range(count):
        customer, identity, conversation = await campaign_recipient(
            stack, organization_id, suffix=suffix_base + index
        )
        await stack.service.set_contact_preference(
            organization_id,
            ContactPreferenceRequest(
                customer_id=customer.id,
                identity_id=identity.id,
                channel=MessageChannel.SMS,
                consent_granted=True,
                evidence_ref=f"consent:corrective:{index}",
            ),
        )
        members.append(
            AudienceMemberInput(
                customer_id=customer.id,
                identity_id=identity.id,
                conversation_id=conversation.id,
            )
        )
    draft = CampaignDraft(
        release_workflow_version_id=release.id,
        recipient_workflow_version_id=recipient_workflow.id,
        account_id=account.id,
        channel=MessageChannel.SMS,
        content=MessageContent(text="Corrective campaign"),
        audience=tuple(members),
        throttle=throttle,
    )
    campaign = await stack.service.create_campaign(
        organization_id,
        CreateCampaignRequest(
            campaign_key=f"campaign.corrective.{uuid.uuid4().hex[:8]}",
            name="Corrective",
            draft=draft,
        ),
    )
    return await stack.service.prepare_campaign(organization_id, campaign.id)


async def _release_started(stack: Any, organization_id: uuid.UUID, campaign: Any) -> Any:
    run = await stack.service.start_campaign(organization_id, campaign.id)
    assert run.release_workflow_run_id is not None
    await stack.workflows.execute_next(
        campaign_principal(organization_id), run.release_workflow_run_id
    )
    return run


async def _running_members(
    stack: Any,
    organization_id: uuid.UUID,
    *,
    count: int,
    throttle: ThrottlePolicy,
    suffix_base: int = 200,
) -> tuple[Any, Any]:
    campaign = await _prepared_members(
        stack,
        organization_id,
        count=count,
        throttle=throttle,
        suffix_base=suffix_base,
    )
    run = await _release_started(stack, organization_id, campaign)
    return campaign, await stack.service.confirm_release(organization_id, run.id)


async def _ready_claims(stack: Any, organization_id: uuid.UUID, run: Any, count: int) -> list[Any]:
    claims = []
    for _ in range(count):
        claims.append(await ready_claim(stack, organization_id, run))
    return claims


async def test_duplicate_schedule_request_reuses_one_p16_owned_binding(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    campaign, _ = await prepared_campaign(campaign_stack, organization.id)
    start_at = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    request = ScheduleCampaignRequest(
        schedule_type=ScheduleType.ONE_TIME,
        timezone="UTC",
        start_at=start_at,
    )
    first = await campaign_stack.service.schedule_campaign(organization.id, campaign.id, request)
    second = await campaign_stack.service.schedule_campaign(organization.id, campaign.id, request)
    runs = await campaign_stack.service.runs(organization.id, campaign.id, limit=10)
    schedules = await campaign_stack.scheduler.list_schedules(organization.id, limit=10, offset=0)
    assert first.state is CampaignState.SCHEDULED
    assert second.state is CampaignState.SCHEDULED
    assert len(runs) == 1
    assert len(schedules) == 1
    assert runs[0].schedule_id == schedules[0].id
    assert schedules[0].input["campaign_run_id"] == str(runs[0].id)


async def test_direct_recurring_campaign_schedule_is_fenced_without_durable_side_effects(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    campaign, _ = await prepared_campaign(campaign_stack, organization.id)
    invalid = ScheduleCampaignRequest.model_construct(
        schedule_type=ScheduleType.RECURRING,
        timezone="UTC",
        start_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
        end_at=None,
        recurrence=RecurrenceSpec(frequency=RecurrenceFrequency.MINUTELY),
        misfire_policy=MisfirePolicy.FIRE_ONCE,
        max_catch_up=1,
    )

    with pytest.raises(
        CampaignInvalidStateError, match="Campaign scheduling supports ONE_TIME only"
    ):
        await campaign_stack.service.schedule_campaign(organization.id, campaign.id, invalid)

    assert await campaign_stack.service.runs(organization.id, campaign.id, limit=10) == []
    assert await campaign_stack.scheduler.list_schedules(organization.id, limit=10, offset=0) == []


async def test_foreign_schedule_key_with_wrong_workflow_is_rejected(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    campaign, _ = await prepared_campaign(campaign_stack, organization.id)
    wrong_workflow = await campaign_workflow(campaign_stack, organization.id, "wrong-release")
    start_at = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    schedule_key = f"campaign.{campaign.id.hex}.r{campaign.revision}"
    await campaign_stack.scheduler.create_schedule(
        organization.id,
        CreateScheduleRequest(
            schedule_key=schedule_key,
            workflow_version_id=wrong_workflow.id,
            schedule_type=ScheduleType.ONE_TIME,
            timezone="UTC",
            start_at=start_at,
            input={"foreign": True},
        ),
    )
    with pytest.raises(CampaignExecutionFencedError, match="immutable release binding"):
        await campaign_stack.service.schedule_campaign(
            organization.id,
            campaign.id,
            ScheduleCampaignRequest(
                schedule_type=ScheduleType.ONE_TIME,
                timezone="UTC",
                start_at=start_at,
            ),
        )


async def test_cross_tenant_schedule_binding_is_rejected_by_composite_fk(
    campaign_stack: Any, make_organization: Any
) -> None:
    first, second = await make_organization(), await make_organization()
    campaign, _ = await prepared_campaign(campaign_stack, first.id)
    foreign_workflow = await campaign_workflow(campaign_stack, second.id, "foreign-schedule")
    foreign_schedule = await campaign_stack.scheduler.create_schedule(
        second.id,
        CreateScheduleRequest(
            schedule_key="campaign.foreign.schedule",
            workflow_version_id=foreign_workflow.id,
            schedule_type=ScheduleType.ONE_TIME,
            timezone="UTC",
            start_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
        ),
    )
    async with campaign_stack.database.tenant_transaction(first.id) as tenant:
        repo = CampaignRepository(tenant)
        row = await repo.campaign_row(campaign.id, for_update=True)
        assert row is not None
        run = await repo.create_run(row, idempotency_key="campaign:cross-tenant-schedule")
        with pytest.raises(IntegrityError):
            await repo.bind_schedule(run.id, foreign_schedule.id)


@pytest.mark.parametrize(
    ("policy", "preconsumed"),
    [
        (
            ThrottlePolicy(
                messages_per_minute=1,
                campaign_limit=10,
                organization_messages_per_minute=10,
            ),
            0,
        ),
        (
            ThrottlePolicy(
                messages_per_minute=10,
                campaign_limit=2,
                organization_messages_per_minute=10,
            ),
            1,
        ),
    ],
)
async def test_campaign_limits_do_not_oversubscribe_under_two_workers(
    campaign_stack: Any,
    make_organization: Any,
    policy: ThrottlePolicy,
    preconsumed: int,
) -> None:
    organization = await make_organization()
    _, run = await _running_members(campaign_stack, organization.id, count=2, throttle=policy)
    claims = await _ready_claims(campaign_stack, organization.id, run, 2)
    if preconsumed:
        async with campaign_stack.database.tenant_transaction(organization.id) as tenant:
            stored_run = (
                await tenant.session.execute(
                    select(CampaignRunRecord).where(CampaignRunRecord.id == run.id)
                )
            ).scalar_one()
            stored_run.authorized_count = preconsumed
    results = await asyncio.gather(
        *(campaign_stack.service.authorize_send(organization.id, claim) for claim in claims),
        return_exceptions=True,
    )
    assert len([result for result in results if not isinstance(result, BaseException)]) == 1
    assert len([result for result in results if isinstance(result, CampaignInvalidStateError)]) == 1
    async with campaign_stack.database.tenant_transaction(organization.id) as tenant:
        stored_run = (
            await tenant.session.execute(
                select(CampaignRunRecord).where(CampaignRunRecord.id == run.id)
            )
        ).scalar_one()
        run_window = (
            await tenant.session.execute(select(CampaignThrottleWindowRecord))
        ).scalar_one()
    assert stored_run.authorized_count == preconsumed + 1
    assert run_window.reserved_count == 1


async def test_organization_minute_limit_serializes_distinct_campaign_runs(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    policy = ThrottlePolicy(
        messages_per_minute=10,
        campaign_limit=10,
        organization_messages_per_minute=1,
    )
    _, first_run = await _running_members(
        campaign_stack, organization.id, count=1, throttle=policy, suffix_base=300
    )
    _, second_run = await _running_members(
        campaign_stack, organization.id, count=1, throttle=policy, suffix_base=400
    )
    first_claim = (await _ready_claims(campaign_stack, organization.id, first_run, 1))[0]
    second_claim = (await _ready_claims(campaign_stack, organization.id, second_run, 1))[0]
    results = await asyncio.gather(
        campaign_stack.service.authorize_send(organization.id, first_claim),
        campaign_stack.service.authorize_send(organization.id, second_claim),
        return_exceptions=True,
    )
    assert len([result for result in results if not isinstance(result, BaseException)]) == 1
    assert len([result for result in results if isinstance(result, CampaignInvalidStateError)]) == 1
    async with campaign_stack.database.tenant_transaction(organization.id) as tenant:
        organization_window = (
            await tenant.session.execute(select(CampaignOrganizationThrottleWindowRecord))
        ).scalar_one()
    assert organization_window.reserved_count == 1


async def test_attempt_materialization_is_bounded_and_resumes_from_durable_cursor(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    campaign = await _prepared_members(
        campaign_stack,
        organization.id,
        count=2,
        throttle=ThrottlePolicy(),
        suffix_base=500,
    )
    run = await _release_started(campaign_stack, organization.id, campaign)
    async with campaign_stack.database.tenant_transaction(organization.id) as tenant:
        repo = CampaignRepository(tenant)
        materializing = await repo.begin_run_activation(run.id)
        first_batch, created = await repo.materialize_attempt_batch(run.id, limit=1)
    assert materializing.state is CampaignRunState.MATERIALIZING
    assert created == 1
    assert first_batch.state is CampaignRunState.MATERIALIZING
    assert first_batch.attempt_materialization_cursor == 1
    assert first_batch.attempt_materialization_complete is False
    resumed = await campaign_stack.service.confirm_release(organization.id, run.id)
    async with campaign_stack.database.tenant_transaction(organization.id) as tenant:
        count = int(
            (
                await tenant.session.execute(
                    select(func.count(CampaignRecipientAttemptRecord.id)).where(
                        CampaignRecipientAttemptRecord.campaign_run_id == run.id
                    )
                )
            ).scalar_one()
        )
    assert resumed.state is CampaignRunState.RUNNING
    assert resumed.attempt_materialization_cursor == 2
    assert resumed.attempt_materialization_complete is True
    assert count == 2


async def test_pause_fences_resumable_attempt_materialization(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    campaign = await _prepared_members(
        campaign_stack,
        organization.id,
        count=2,
        throttle=ThrottlePolicy(),
        suffix_base=550,
    )
    run = await _release_started(campaign_stack, organization.id, campaign)
    async with campaign_stack.database.tenant_transaction(organization.id) as tenant:
        repo = CampaignRepository(tenant)
        await repo.begin_run_activation(run.id)
        materializing, created = await repo.materialize_attempt_batch(run.id, limit=1)
    assert materializing.state is CampaignRunState.MATERIALIZING
    assert created == 1
    await campaign_stack.service.pause_campaign(organization.id, campaign.id)
    with pytest.raises(CampaignInvalidStateError, match="does not authorize materialization"):
        await campaign_stack.service.confirm_release(organization.id, run.id)
    await campaign_stack.service.resume_campaign(organization.id, campaign.id)
    resumed = await campaign_stack.service.confirm_release(organization.id, run.id)
    assert resumed.state is CampaignRunState.RUNNING
    assert resumed.attempt_materialization_cursor == 2


async def test_cancellation_batches_are_durable_resumable_and_idempotent(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    campaign, run = await _running_members(
        campaign_stack,
        organization.id,
        count=2,
        throttle=ThrottlePolicy(),
        suffix_base=600,
    )
    async with campaign_stack.database.tenant_transaction(organization.id) as tenant:
        repo = CampaignRepository(tenant)
        cancelling = await repo.begin_cancellation(campaign.id)
        first, processed, complete = await repo.cancel_attempt_batch(campaign.id, limit=1)
    assert cancelling.state is CampaignState.CANCELLING
    assert first.state is CampaignState.CANCELLING
    assert processed == 1
    assert complete is False
    async with campaign_stack.database.tenant_transaction(organization.id) as tenant:
        second, processed, complete = await CampaignRepository(tenant).cancel_attempt_batch(
            campaign.id, limit=1
        )
    assert second.state is CampaignState.CANCELLED
    assert processed == 1
    assert complete is True
    async with campaign_stack.database.tenant_transaction(organization.id) as tenant:
        replay, processed, complete = await CampaignRepository(tenant).cancel_attempt_batch(
            campaign.id, limit=1
        )
        stored_run = (
            await tenant.session.execute(
                select(CampaignRunRecord).where(CampaignRunRecord.id == run.id)
            )
        ).scalar_one()
    assert replay.state is CampaignState.CANCELLED
    assert processed == 0
    assert complete is True
    assert stored_run.state == CampaignRunState.CANCELLED.value
    assert stored_run.cancellation_processed_count == 2
    assert stored_run.cancellation_complete is True


async def test_p15_corrective_misfire_accounting_remains_available_outside_p16(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    campaign, _ = await prepared_campaign(campaign_stack, organization.id)
    assert campaign.prepared_revision_id is not None
    async with campaign_stack.database.tenant_transaction(organization.id) as tenant:
        revision = await CampaignRepository(tenant).revision_row(campaign.prepared_revision_id)
    assert revision is not None
    schedule = await campaign_stack.scheduler.create_schedule(
        organization.id,
        CreateScheduleRequest(
            schedule_key=f"p15.campaign-scope-regression.{campaign.id.hex[:8]}",
            workflow_version_id=revision.release_workflow_version_id,
            schedule_type=ScheduleType.RECURRING,
            timezone="UTC",
            start_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10),
            recurrence=RecurrenceSpec(frequency=RecurrenceFrequency.MINUTELY),
            input={"p15_general_recurrence": True},
            misfire_policy=MisfirePolicy.FIRE_ONCE,
        ),
    )
    schedule = await campaign_stack.scheduler.activate_schedule(organization.id, schedule.id)
    occurrences = await campaign_stack.scheduler.materialize_due(organization.id)
    async with campaign_stack.database.tenant_transaction(organization.id) as tenant:
        accounting = (
            (
                await tenant.session.execute(
                    select(SchedulerTransitionHistoryRecord).where(
                        SchedulerTransitionHistoryRecord.schedule_id == schedule.id,
                        SchedulerTransitionHistoryRecord.reason_code
                        == "MISFIRE_FIRE_ONCE_COALESCED",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(occurrences) == 1
    assert len(accounting) == 1


async def test_scheduled_release_dispatch_advances_bound_campaign_run(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    campaign, _ = await prepared_campaign(campaign_stack, organization.id)
    await campaign_stack.service.schedule_campaign(
        organization.id,
        campaign.id,
        ScheduleCampaignRequest(
            schedule_type=ScheduleType.ONE_TIME,
            timezone="UTC",
            start_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1),
        ),
    )
    run = (await campaign_stack.service.runs(organization.id, campaign.id, limit=10))[0]
    occurrences = await campaign_stack.scheduler.materialize_due(organization.id)
    assert len(occurrences) == 1
    claim = await campaign_stack.scheduler.claim_due(organization.id, uuid.uuid7())
    assert claim is not None
    dispatched = await campaign_stack.scheduler.dispatch_claim(organization.id, claim)
    assert dispatched.workflow_run_id is not None
    await campaign_stack.workflows.execute_next(
        campaign_principal(organization.id), dispatched.workflow_run_id
    )
    bound = await campaign_stack.service.bridge_scheduled_release(
        organization.id,
        run.id,
        dispatched.id,
        dispatched.workflow_run_id,
    )

    confirmed = await campaign_stack.service.confirm_release(organization.id, run.id)
    async with campaign_stack.database.tenant_transaction(organization.id) as tenant:
        attempts = (
            (
                await tenant.session.execute(
                    select(CampaignRecipientAttemptRecord).where(
                        CampaignRecipientAttemptRecord.campaign_run_id == run.id
                    )
                )
            )
            .scalars()
            .all()
        )
        binding_history = (
            (
                await tenant.session.execute(
                    select(CampaignTransitionHistoryRecord).where(
                        CampaignTransitionHistoryRecord.entity_id == run.id,
                        CampaignTransitionHistoryRecord.reason_code == "SCHEDULED_RELEASE_BOUND",
                    )
                )
            )
            .scalars()
            .all()
        )

    assert bound.release_schedule_occurrence_id == dispatched.id
    assert bound.release_workflow_run_id == dispatched.workflow_run_id
    assert confirmed.state in {CampaignRunState.MATERIALIZING, CampaignRunState.RUNNING}
    assert len(attempts) == 1
    assert attempts[0].campaign_run_id == run.id
    assert len(binding_history) == 1
