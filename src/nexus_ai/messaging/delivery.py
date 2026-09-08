"""Canonical delivery state machine (NXS-P09, ADR-0072).

A channel adapter maps a provider status string to a canonical :class:`MessageStatus`;
this module owns the ONE definition of which transitions are legal. Rules:

* forward progress only along RECEIVED/QUEUED/SENDING/SENT/DELIVERED/READ, plus FAILED
  from any non-terminal state;
* an out-of-order or duplicate callback that would *regress* the canonical state
  (e.g. a late ``sent`` after ``read``) is safely IGNORED, never applied;
* re-applying the current state is idempotent (no-op, no error);
* READ and FAILED are terminal — nothing transitions out of them;
* an explicit illegal transition requested by an API caller raises
  :class:`MessagingStateConflictError`; a provider callback never raises, it just
  reports whether it advanced the state.
"""

from __future__ import annotations

from dataclasses import dataclass

from nexus_ai.messaging.entities import TERMINAL_STATUSES, MessageStatus
from nexus_ai.messaging.errors import MessagingStateConflictError

_RANK: dict[MessageStatus, int] = {
    MessageStatus.RECEIVED: 0,
    MessageStatus.QUEUED: 1,
    MessageStatus.SENDING: 2,
    MessageStatus.SENT: 3,
    MessageStatus.DELIVERED: 4,
    MessageStatus.READ: 5,
    MessageStatus.FAILED: 5,
}


@dataclass(frozen=True, slots=True)
class TransitionOutcome:
    #: The state after applying the callback (unchanged when the callback was ignored).
    status: MessageStatus
    #: True when the callback moved the canonical state forward.
    advanced: bool
    #: True when the callback matched the current state exactly (idempotent replay).
    duplicate: bool


def can_transition(current: MessageStatus, target: MessageStatus) -> bool:
    if current in TERMINAL_STATUSES:
        return current is target
    if target is MessageStatus.FAILED:
        return True
    return _RANK[target] > _RANK[current]


def apply_callback(current: MessageStatus, reported: MessageStatus) -> TransitionOutcome:
    """Fold a provider delivery-status callback into the canonical state. Never raises —
    a regressive or out-of-order callback is ignored."""
    if reported is current:
        return TransitionOutcome(status=current, advanced=False, duplicate=True)
    if current in TERMINAL_STATUSES:
        return TransitionOutcome(status=current, advanced=False, duplicate=False)
    if reported is MessageStatus.FAILED or _RANK[reported] > _RANK[current]:
        return TransitionOutcome(status=reported, advanced=True, duplicate=False)
    return TransitionOutcome(status=current, advanced=False, duplicate=False)


def require_transition(current: MessageStatus, target: MessageStatus) -> None:
    """Assert a caller-requested transition is legal, or raise."""
    if not can_transition(current, target):
        raise MessagingStateConflictError(
            f"a message in {current.value} cannot transition to {target.value}",
            extensions={"current_status": current.value, "requested_status": target.value},
        )
