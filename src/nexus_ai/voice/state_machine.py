"""The voice-session state machine (NXS-P12, ADR-0089).

Deterministic, monotonic and fail-closed — the same design as the NXS-P11 telephony call
state machine (ADR-0085). A provider event proposes a :class:`VoiceSessionState`;
:func:`fold_session_state` decides whether it is applied or ignored (stale /
out-of-order / duplicate / post-terminal).

**The live lifecycle is linear** (PENDING -> CONNECTING -> CONNECTED -> STREAMING ->
ENDING), so rank is a bijection with the live state and there is no "illegal live
transition": a higher-rank event applies (inferring any reordered / dropped
intermediate), a lower-rank one is ignored as stale.

**Provider-order precedence (enforced).** A non-terminal provider event the provider
orders strictly before the recorded fact (a lower ``provider_sequence`` when both carry
one, else an older ``provider_timestamp`` when both carry one) is a reordered stale
callback and is ignored EVEN AT HIGHER RANK. A terminal outcome is exempt and always
wins.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import Enum, StrEnum


class VoiceSessionState(StrEnum):
    PENDING = "PENDING"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    STREAMING = "STREAMING"
    ENDING = "ENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class VoiceSessionDisposition(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


_RANK: dict[VoiceSessionState, int] = {
    VoiceSessionState.PENDING: 0,
    VoiceSessionState.CONNECTING: 1,
    VoiceSessionState.CONNECTED: 2,
    VoiceSessionState.STREAMING: 3,
    VoiceSessionState.ENDING: 4,
    VoiceSessionState.COMPLETED: 5,
    VoiceSessionState.FAILED: 5,
    VoiceSessionState.CANCELLED: 5,
}

_TERMINAL = frozenset(
    {
        VoiceSessionState.COMPLETED,
        VoiceSessionState.FAILED,
        VoiceSessionState.CANCELLED,
    }
)

_DISPOSITION_FOR: dict[VoiceSessionState, VoiceSessionDisposition] = {
    VoiceSessionState.COMPLETED: VoiceSessionDisposition.COMPLETED,
    VoiceSessionState.FAILED: VoiceSessionDisposition.FAILED,
    VoiceSessionState.CANCELLED: VoiceSessionDisposition.CANCELLED,
}


def session_rank(state: VoiceSessionState) -> int:
    return _RANK[state]


def is_terminal(state: VoiceSessionState) -> bool:
    return state in _TERMINAL


class FoldOutcome(Enum):
    APPLIED = "APPLIED"
    #: Older / lower-rank / duplicate / post-terminal — a deterministic no-op.
    IGNORED = "IGNORED"


@dataclass(frozen=True, slots=True)
class FoldResult:
    outcome: FoldOutcome
    state: VoiceSessionState
    disposition: VoiceSessionDisposition | None
    reason: str


def fold_session_state(
    *,
    current: VoiceSessionState,
    current_rank: int,
    current_provider_ts: dt.datetime | None,
    current_sequence: int | None,
    proposed: VoiceSessionState,
    proposed_provider_ts: dt.datetime | None = None,
    proposed_sequence: int | None = None,
) -> FoldResult:
    """Decide how a proposed voice-session state folds into the current state."""
    proposed_rank = _RANK[proposed]

    if current in _TERMINAL:
        if proposed == current:
            return FoldResult(
                FoldOutcome.IGNORED, current, _DISPOSITION_FOR.get(current), "duplicate-terminal"
            )
        return FoldResult(
            FoldOutcome.IGNORED, current, _DISPOSITION_FOR.get(current), "session-already-terminal"
        )

    if proposed == current:
        return FoldResult(FoldOutcome.IGNORED, current, None, "duplicate-state")

    if proposed not in _TERMINAL and _provider_order_precedes(
        proposed_provider_ts, proposed_sequence, current_provider_ts, current_sequence
    ):
        return FoldResult(FoldOutcome.IGNORED, current, None, "stale-provider-order")

    if proposed_rank < current_rank:
        return FoldResult(FoldOutcome.IGNORED, current, None, "stale-lower-rank")

    if proposed_rank == current_rank:
        # Unreachable for live states (rank is a bijection with the live state, and
        # proposed == current is handled above). Defensive idempotent no-op.
        return FoldResult(FoldOutcome.IGNORED, current, None, "same-rank-no-change")

    if proposed in _TERMINAL:
        return FoldResult(
            FoldOutcome.APPLIED, proposed, _DISPOSITION_FOR[proposed], "terminal-transition"
        )
    return FoldResult(FoldOutcome.APPLIED, proposed, None, "forward-transition")


def _provider_order_precedes(
    ts_a: dt.datetime | None,
    seq_a: int | None,
    ts_b: dt.datetime | None,
    seq_b: int | None,
) -> bool:
    """True only when event A is PROVABLY earlier than event B in the provider's own
    ordering: a strictly lower sequence (both present) or, failing that, a strictly older
    timestamp (both present). Otherwise the events are not comparable."""
    if seq_a is not None and seq_b is not None:
        return seq_a < seq_b
    if ts_a is not None and ts_b is not None:
        return ts_a < ts_b
    return False
