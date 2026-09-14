"""Closed, absorbing Campaign state machines."""

from enum import StrEnum

from nexus_ai.campaigns.errors import CampaignInvalidStateError


class CampaignState(StrEnum):
    DRAFT = "DRAFT"
    PREPARING = "PREPARING"
    READY = "READY"
    SCHEDULED = "SCHEDULED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SnapshotState(StrEnum):
    OPEN = "OPEN"
    SEALED = "SEALED"
    FAILED = "FAILED"


class RecipientState(StrEnum):
    PENDING = "PENDING"
    ELIGIBLE = "ELIGIBLE"
    DEFERRED = "DEFERRED"
    SUPPRESSED = "SUPPRESSED"
    CANCELLED = "CANCELLED"


class CampaignRunState(StrEnum):
    PENDING_RELEASE = "PENDING_RELEASE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RecipientAttemptState(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    WORKFLOW_RUNNING = "WORKFLOW_RUNNING"
    READY_TO_SEND = "READY_TO_SEND"
    DISPATCH_AUTHORIZED = "DISPATCH_AUTHORIZED"
    DISPATCHED = "DISPATCHED"
    SUPPRESSED = "SUPPRESSED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SendPermitState(StrEnum):
    PENDING = "PENDING"
    AUTHORIZED = "AUTHORIZED"
    CONSUMED = "CONSUMED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


CAMPAIGN_TERMINAL = frozenset(
    {CampaignState.COMPLETED, CampaignState.FAILED, CampaignState.CANCELLED}
)
RUN_TERMINAL = frozenset(
    {CampaignRunState.COMPLETED, CampaignRunState.FAILED, CampaignRunState.CANCELLED}
)

_CAMPAIGN_TRANSITIONS = {
    CampaignState.DRAFT: {CampaignState.PREPARING, CampaignState.CANCELLED},
    CampaignState.PREPARING: {CampaignState.READY, CampaignState.DRAFT, CampaignState.CANCELLED},
    CampaignState.READY: {CampaignState.SCHEDULED, CampaignState.RUNNING, CampaignState.CANCELLED},
    CampaignState.SCHEDULED: {CampaignState.RUNNING, CampaignState.FAILED, CampaignState.CANCELLED},
    CampaignState.RUNNING: {
        CampaignState.PAUSED,
        CampaignState.COMPLETED,
        CampaignState.FAILED,
        CampaignState.CANCELLED,
    },
    CampaignState.PAUSED: {
        CampaignState.RUNNING,
        CampaignState.FAILED,
        CampaignState.CANCELLED,
    },
    CampaignState.COMPLETED: set(),
    CampaignState.FAILED: set(),
    CampaignState.CANCELLED: set(),
}


def require_campaign_transition(current: CampaignState, target: CampaignState) -> None:
    if target not in _CAMPAIGN_TRANSITIONS[current]:
        raise CampaignInvalidStateError(f"campaign cannot transition from {current} to {target}")


def resolve_attempt_transition(
    current: RecipientAttemptState, target: RecipientAttemptState
) -> None:
    allowed = {
        RecipientAttemptState.PENDING: {
            RecipientAttemptState.CLAIMED,
            RecipientAttemptState.SUPPRESSED,
            RecipientAttemptState.CANCELLED,
        },
        RecipientAttemptState.CLAIMED: {
            RecipientAttemptState.WORKFLOW_RUNNING,
            RecipientAttemptState.SUPPRESSED,
            RecipientAttemptState.FAILED,
        },
        RecipientAttemptState.WORKFLOW_RUNNING: {
            RecipientAttemptState.READY_TO_SEND,
            RecipientAttemptState.SUPPRESSED,
            RecipientAttemptState.FAILED,
        },
        RecipientAttemptState.READY_TO_SEND: {
            RecipientAttemptState.DISPATCH_AUTHORIZED,
            RecipientAttemptState.SUPPRESSED,
            RecipientAttemptState.FAILED,
        },
        RecipientAttemptState.DISPATCH_AUTHORIZED: {
            RecipientAttemptState.DISPATCHED,
            RecipientAttemptState.FAILED,
        },
        RecipientAttemptState.DISPATCHED: set(),
        RecipientAttemptState.SUPPRESSED: set(),
        RecipientAttemptState.FAILED: set(),
        RecipientAttemptState.CANCELLED: set(),
    }
    if target not in allowed[current]:
        raise CampaignInvalidStateError(
            f"recipient attempt cannot transition from {current} to {target}"
        )
