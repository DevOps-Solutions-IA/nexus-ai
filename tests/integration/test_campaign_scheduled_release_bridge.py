"""NXS-P16 corrective #2 scheduled-release bridge over real PostgreSQL."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from nexus_ai.campaigns.entities import ScheduleCampaignRequest
from nexus_ai.campaigns.errors import CampaignExecutionFencedError, CampaignInvalidStateError
from nexus_ai.campaigns.state_machine import CampaignRunState, CampaignState
from nexus_ai.domain.campaigns.models import CampaignRecord, CampaignRunRecord
from nexus_ai.scheduler.entities import ScheduleOccurrence, ScheduleType
from nexus_ai.workflows.entities import StartWorkflowRunRequest
from tests.integration.test_campaign_service import (
    campaign_principal,
    campaign_workflow,
    prepared_campaign,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _dispatch_scheduled_release(
    stack: Any, organization_id: uuid.UUID
) -> tuple[Any, Any, ScheduleOccurrence]:
    campaign, _ = await prepared_campaign(stack, organization_id)
    await stack.service.schedule_campaign(
        organization_id,
        campaign.id,
        ScheduleCampaignRequest(
            schedule_type=ScheduleType.ONE_TIME,
            timezone="UTC",
            start_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1),
        ),
    )
    run = (await stack.service.runs(organization_id, campaign.id, limit=10))[0]
    occurrences = await stack.scheduler.materialize_due(organization_id)
    assert len(occurrences) == 1
    claim = await stack.scheduler.claim_due(organization_id, uuid.uuid7())
    assert claim is not None and claim.occurrence.id == occurrences[0].id
    dispatched = await stack.scheduler.dispatch_claim(organization_id, claim)
    assert dispatched.workflow_run_id is not None
    return campaign, run, dispatched


async def _bind(stack: Any, organization_id: uuid.UUID, run: Any, occurrence: Any) -> Any:
    assert occurrence.workflow_run_id is not None
    return await stack.service.bridge_scheduled_release(
        organization_id,
        run.id,
        occurrence.id,
        occurrence.workflow_run_id,
    )


async def test_duplicate_bridge_delivery_is_idempotent_under_two_workers(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    _, run, occurrence = await _dispatch_scheduled_release(campaign_stack, organization.id)

    first, second = await asyncio.gather(
        _bind(campaign_stack, organization.id, run, occurrence),
        _bind(campaign_stack, organization.id, run, occurrence),
    )

    assert first.release_schedule_occurrence_id == occurrence.id
    assert second.release_schedule_occurrence_id == occurrence.id
    assert first.release_workflow_run_id == second.release_workflow_run_id


async def test_different_workflow_run_cannot_replace_scheduled_release_binding(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    _, run, occurrence = await _dispatch_scheduled_release(campaign_stack, organization.id)
    alternate = await campaign_stack.workflows.start_run(
        organization.id,
        StartWorkflowRunRequest(
            workflow_version_id=occurrence.workflow_version_id,
            input={
                "campaign_id": str(run.campaign_id),
                "campaign_revision_id": str(run.campaign_revision_id),
                "campaign_run_id": str(run.id),
            },
            idempotency_key=f"campaign:alternate-release:{run.id}",
        ),
    )

    with pytest.raises(CampaignExecutionFencedError, match="workflow run identity"):
        await campaign_stack.service.bridge_scheduled_release(
            organization.id, run.id, occurrence.id, alternate.id
        )


async def test_wrong_workflow_version_is_rejected(
    campaign_stack: Any, make_organization: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    organization = await make_organization()
    _, run, occurrence = await _dispatch_scheduled_release(campaign_stack, organization.id)
    wrong_version = await campaign_workflow(campaign_stack, organization.id, "bridge-wrong-version")
    wrong_run = await campaign_stack.workflows.start_run(
        organization.id,
        StartWorkflowRunRequest(
            workflow_version_id=wrong_version.id,
            input={
                "campaign_id": str(run.campaign_id),
                "campaign_revision_id": str(run.campaign_revision_id),
                "campaign_run_id": str(run.id),
            },
            idempotency_key=f"campaign:wrong-version:{run.id}",
        ),
    )
    corrupted = occurrence.model_copy(
        update={"workflow_version_id": wrong_version.id, "workflow_run_id": wrong_run.id}
    )

    async def _wrong_occurrence(_organization_id: uuid.UUID, _occurrence_id: uuid.UUID) -> Any:
        return corrupted

    monkeypatch.setattr(campaign_stack.scheduler, "get_occurrence", _wrong_occurrence)
    with pytest.raises(CampaignExecutionFencedError, match="workflow version"):
        await campaign_stack.service.bridge_scheduled_release(
            organization.id, run.id, occurrence.id, wrong_run.id
        )


async def test_occurrence_from_another_schedule_is_rejected(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    _, first_run, _ = await _dispatch_scheduled_release(campaign_stack, organization.id)
    _, _, second_occurrence = await _dispatch_scheduled_release(campaign_stack, organization.id)
    assert second_occurrence.workflow_run_id is not None

    with pytest.raises(CampaignExecutionFencedError, match="another schedule"):
        await campaign_stack.service.bridge_scheduled_release(
            organization.id,
            first_run.id,
            second_occurrence.id,
            second_occurrence.workflow_run_id,
        )


async def test_cross_tenant_occurrence_and_workflow_are_rejected(
    campaign_stack: Any, make_organization: Any
) -> None:
    owner = await make_organization()
    foreign = await make_organization()
    _, owner_run, owner_occurrence = await _dispatch_scheduled_release(campaign_stack, owner.id)
    _, _, foreign_occurrence = await _dispatch_scheduled_release(campaign_stack, foreign.id)
    assert owner_occurrence.workflow_run_id is not None
    assert foreign_occurrence.workflow_run_id is not None

    with pytest.raises(CampaignExecutionFencedError, match="inaccessible execution"):
        await campaign_stack.service.bridge_scheduled_release(
            owner.id,
            owner_run.id,
            foreign_occurrence.id,
            foreign_occurrence.workflow_run_id,
        )
    with pytest.raises(CampaignExecutionFencedError, match="inaccessible execution"):
        await campaign_stack.service.bridge_scheduled_release(
            owner.id,
            owner_run.id,
            owner_occurrence.id,
            foreign_occurrence.workflow_run_id,
        )


async def test_cross_tenant_occurrence_binding_is_rejected_by_postgresql(
    campaign_stack: Any, make_organization: Any
) -> None:
    owner = await make_organization()
    foreign = await make_organization()
    _, owner_run, owner_occurrence = await _dispatch_scheduled_release(campaign_stack, owner.id)
    _, _, foreign_occurrence = await _dispatch_scheduled_release(campaign_stack, foreign.id)
    assert owner_occurrence.workflow_run_id is not None

    with pytest.raises(IntegrityError):
        async with campaign_stack.database.tenant_transaction(owner.id) as tenant:
            row = await tenant.session.get(CampaignRunRecord, owner_run.id)
            assert row is not None
            row.release_workflow_run_id = owner_occurrence.workflow_run_id
            row.release_schedule_occurrence_id = foreign_occurrence.id


async def test_cancelled_campaign_cannot_bind_scheduled_release(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    campaign, run, occurrence = await _dispatch_scheduled_release(campaign_stack, organization.id)
    cancelled = await campaign_stack.service.cancel_campaign(organization.id, campaign.id)
    assert cancelled.state is CampaignState.CANCELLED

    with pytest.raises(CampaignInvalidStateError):
        await _bind(campaign_stack, organization.id, run, occurrence)


async def test_paused_campaign_cannot_bind_scheduled_release(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    campaign, run, occurrence = await _dispatch_scheduled_release(campaign_stack, organization.id)
    async with campaign_stack.database.tenant_transaction(organization.id) as tenant:
        row = await tenant.session.get(CampaignRecord, campaign.id)
        assert row is not None
        row.state = CampaignState.PAUSED.value

    with pytest.raises(CampaignInvalidStateError, match="does not authorize"):
        await _bind(campaign_stack, organization.id, run, occurrence)


async def test_bridge_before_workflow_completion_binds_but_does_not_release(
    campaign_stack: Any, make_organization: Any
) -> None:
    organization = await make_organization()
    _, run, occurrence = await _dispatch_scheduled_release(campaign_stack, organization.id)
    bound = await _bind(campaign_stack, organization.id, run, occurrence)
    assert bound.state is CampaignRunState.PENDING_RELEASE

    with pytest.raises(CampaignInvalidStateError, match="not complete"):
        await campaign_stack.service.confirm_release(organization.id, run.id)

    assert occurrence.workflow_run_id is not None
    await campaign_stack.workflows.execute_next(
        campaign_principal(organization.id), occurrence.workflow_run_id
    )
    confirmed = await campaign_stack.service.confirm_release(organization.id, run.id)
    assert confirmed.state in {CampaignRunState.MATERIALIZING, CampaignRunState.RUNNING}


async def test_duplicate_p15_dispatch_reuses_exact_p14_run_before_bridge(
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
    await campaign_stack.scheduler.materialize_due(organization.id)
    claim = await campaign_stack.scheduler.claim_due(organization.id, uuid.uuid7())
    assert claim is not None
    first = await campaign_stack.workflows.start_run(
        organization.id,
        StartWorkflowRunRequest(
            workflow_version_id=claim.occurrence.workflow_version_id,
            input=claim.workflow_input,
            idempotency_key=claim.occurrence.p14_idempotency_key,
        ),
    )
    dispatched = await campaign_stack.scheduler.dispatch_claim(organization.id, claim)
    assert dispatched.workflow_run_id == first.id

    bound = await _bind(campaign_stack, organization.id, run, dispatched)
    assert bound.release_workflow_run_id == first.id
