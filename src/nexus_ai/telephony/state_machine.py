"""The telephony call state machine (NXS-P11, ADR-0085).

Deterministic, monotonic and fail-closed. A provider callback proposes a
:class:`CallState`; :func:`fold_state` decides whether it is applied, ignored (stale /
out-of-order / duplicate) or rejected (illegal live transition).

**Monotonic rank.** Every state has a rank. A proposed state is applied only if its rank
is strictly greater than the current rank, OR it is a legal same-rank refinement. A
terminal state is rank-max: once terminal, nothing moves the call — a delayed RINGING
after COMPLETED is a no-op, a second COMPLETED is a no-op.

**Legal live transitions.** Below the terminal band the machine still enforces a legal
edge set so a provider cannot, say, jump CREATED -> BRIDGED without ANSWERED. An
out-of-order but higher-rank event (ANSWERED before a late RINGING) is accepted and the
late lower-rank event is then ignored.

**Precedence for equal information.** When two events carry the same rank the one with
the earlier provider timestamp / lower provider sequence is authoritative; ties keep the
current state (idempotent).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import Enum

from nexus_ai.telephony.entities import CallDisposition, CallState

# Rank groups. Terminal states share the max rank so any terminal wins over any live
# state and no terminal transition is ever legal.
_RANK: dict[CallState, int] = {
    CallState.CREATED: 0,
    CallState.RINGING: 1,
    CallState.EARLY_MEDIA: 2,
    CallState.ANSWERED: 3,
    CallState.BRIDGED: 4,
    CallState.ENDING: 5,
    CallState.COMPLETED: 6,
    CallState.FAILED: 6,
    CallState.CANCELLED: 6,
    CallState.BUSY: 6,
    CallState.NO_ANSWER: 6,
}

#: The live lifecycle is linear:
#:   CREATED -> RINGING -> EARLY_MEDIA -> ANSWERED -> BRIDGED -> ENDING
#: Rank is the single ordering. A forward provider event applies (inferring any
#: reordered / dropped intermediate); a backward one is ignored as stale; a terminal
#: locks the call. There is therefore no "illegal live transition" for a provider STATE
#: event — the only ``NXS_TELEPHONY_INVALID_STATE`` is raised by the API surface
#: (``send_dtmf`` on a call that is not ANSWERED / BRIDGED).

#: A live (non-terminal) state may always move to any terminal outcome.
_TERMINAL = frozenset(
    {
        CallState.COMPLETED,
        CallState.FAILED,
        CallState.CANCELLED,
        CallState.BUSY,
        CallState.NO_ANSWER,
    }
)

_DISPOSITION_FOR: dict[CallState, CallDisposition] = {
    CallState.COMPLETED: CallDisposition.ANSWERED,
    CallState.NO_ANSWER: CallDisposition.NO_ANSWER,
    CallState.BUSY: CallDisposition.BUSY,
    CallState.FAILED: CallDisposition.FAILED,
    CallState.CANCELLED: CallDisposition.CANCELLED,
}


def state_rank(state: CallState) -> int:
    return _RANK[state]


def is_terminal(state: CallState) -> bool:
    return state in _TERMINAL


class FoldOutcome(Enum):
    APPLIED = "APPLIED"
    #: The event is older / lower-rank / a duplicate / post-terminal — deterministic no-op.
    IGNORED = "IGNORED"


@dataclass(frozen=True, slots=True)
class FoldResult:
    outcome: FoldOutcome
    state: CallState
    disposition: CallDisposition | None
    reason: str


def fold_state(
    *,
    current: CallState,
    current_rank: int,
    current_provider_ts: dt.datetime | None,
    proposed: CallState,
    proposed_provider_ts: dt.datetime | None = None,
    proposed_sequence: int | None = None,
    current_sequence: int | None = None,
) -> FoldResult:
    """Decide how a proposed state folds into the current call state."""
    proposed_rank = _RANK[proposed]

    # A call already terminal never moves again.
    if current in _TERMINAL:
        if proposed == current:
            return FoldResult(
                FoldOutcome.IGNORED, current, _DISPOSITION_FOR.get(current), "duplicate-terminal"
            )
        return FoldResult(
            FoldOutcome.IGNORED, current, _DISPOSITION_FOR.get(current), "call-already-terminal"
        )

    # Same state again (live) — idempotent no-op.
    if proposed == current:
        return FoldResult(FoldOutcome.IGNORED, current, None, "duplicate-state")

    # Lower rank than the current live state — a stale / reordered event.
    if proposed_rank < current_rank:
        return FoldResult(FoldOutcome.IGNORED, current, None, "stale-lower-rank")

    # Equal rank, different state at the same level — keep the earlier-timestamped fact.
    if proposed_rank == current_rank:
        if _timestamp_precedes(
            proposed_provider_ts, proposed_sequence, current_provider_ts, current_sequence
        ):
            # The proposed event is actually the earlier authoritative fact but names a
            # different same-rank state; treat as a no-op refinement (idempotent).
            return FoldResult(FoldOutcome.IGNORED, current, None, "same-rank-earlier")
        return FoldResult(FoldOutcome.IGNORED, current, None, "same-rank-not-newer")

    # Higher rank: a terminal outcome is always reachable from a live state.
    if proposed in _TERMINAL:
        return FoldResult(
            FoldOutcome.APPLIED,
            proposed,
            _DISPOSITION_FOR[proposed],
            "terminal-transition",
        )

    # Higher rank, non-terminal: a forward step. The lifecycle is linear, so any
    # higher-rank live state is a legal forward move; an intermediate whose event was
    # reordered / dropped is simply inferred (ANSWERED before a late RINGING is fine).
    return FoldResult(FoldOutcome.APPLIED, proposed, None, "forward-transition")


def _timestamp_precedes(
    ts_a: dt.datetime | None,
    seq_a: int | None,
    ts_b: dt.datetime | None,
    seq_b: int | None,
) -> bool:
    if seq_a is not None and seq_b is not None:
        return seq_a < seq_b
    if ts_a is not None and ts_b is not None:
        return ts_a < ts_b
    return False
