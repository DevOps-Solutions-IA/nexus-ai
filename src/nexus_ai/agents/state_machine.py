"""Agent-session and agent-turn state machines (NXS-P13, ADR-0094).

Deterministic, terminal-safe and fail-closed — the same discipline as the NXS-P11 /
NXS-P12 machines. A terminal state is absorbing (no resurrection). A live transition is
applied only when it is a declared edge in the interaction graph; anything else is a
deterministic no-op. The agent-turn machine drives the bounded model/tool loop; the
agent-session machine reflects the session's controlled interaction context.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, StrEnum


class AgentSessionState(StrEnum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    WAITING_TOOL = "WAITING_TOOL"
    RESPONDING = "RESPONDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AgentSessionDisposition(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class AgentTurnState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    AWAITING_TOOLS = "AWAITING_TOOLS"
    FINALIZING = "FINALIZING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


_SESSION_TERMINAL = frozenset(
    {AgentSessionState.COMPLETED, AgentSessionState.FAILED, AgentSessionState.CANCELLED}
)
_TURN_TERMINAL = frozenset(
    {AgentTurnState.COMPLETED, AgentTurnState.FAILED, AgentTurnState.CANCELLED}
)

#: a live session is one of these — a turn moves it around this working set
_SESSION_LIVE = frozenset(
    {
        AgentSessionState.PENDING,
        AgentSessionState.ACTIVE,
        AgentSessionState.WAITING_TOOL,
        AgentSessionState.RESPONDING,
    }
)

#: rank is used only for the "how far did we get" audit signal, never for ordering.
_SESSION_RANK: dict[AgentSessionState, int] = {
    AgentSessionState.PENDING: 0,
    AgentSessionState.ACTIVE: 1,
    AgentSessionState.WAITING_TOOL: 2,
    AgentSessionState.RESPONDING: 3,
    AgentSessionState.COMPLETED: 4,
    AgentSessionState.FAILED: 4,
    AgentSessionState.CANCELLED: 4,
}

#: the declared live-transition graph for the interaction context
_SESSION_EDGES: dict[AgentSessionState, frozenset[AgentSessionState]] = {
    AgentSessionState.PENDING: frozenset({AgentSessionState.ACTIVE, AgentSessionState.RESPONDING}),
    AgentSessionState.ACTIVE: frozenset(
        {AgentSessionState.WAITING_TOOL, AgentSessionState.RESPONDING}
    ),
    AgentSessionState.WAITING_TOOL: frozenset(
        {AgentSessionState.ACTIVE, AgentSessionState.RESPONDING}
    ),
    AgentSessionState.RESPONDING: frozenset({AgentSessionState.ACTIVE}),
}

_SESSION_DISPOSITION_FOR: dict[AgentSessionState, AgentSessionDisposition] = {
    AgentSessionState.COMPLETED: AgentSessionDisposition.COMPLETED,
    AgentSessionState.FAILED: AgentSessionDisposition.FAILED,
    AgentSessionState.CANCELLED: AgentSessionDisposition.CANCELLED,
}

_TURN_EDGES: dict[AgentTurnState, frozenset[AgentTurnState]] = {
    AgentTurnState.PENDING: frozenset({AgentTurnState.RUNNING}),
    AgentTurnState.RUNNING: frozenset({AgentTurnState.AWAITING_TOOLS, AgentTurnState.FINALIZING}),
    AgentTurnState.AWAITING_TOOLS: frozenset({AgentTurnState.RUNNING, AgentTurnState.FINALIZING}),
    AgentTurnState.FINALIZING: frozenset({AgentTurnState.COMPLETED}),
}


def session_rank(state: AgentSessionState) -> int:
    return _SESSION_RANK[state]


def session_is_terminal(state: AgentSessionState) -> bool:
    return state in _SESSION_TERMINAL


def session_is_live(state: AgentSessionState) -> bool:
    return state in _SESSION_LIVE


def turn_is_terminal(state: AgentTurnState) -> bool:
    return state in _TURN_TERMINAL


class FoldOutcome(Enum):
    APPLIED = "APPLIED"
    #: duplicate / illegal-live / post-terminal — a deterministic no-op.
    IGNORED = "IGNORED"


@dataclass(frozen=True, slots=True)
class SessionFoldResult:
    outcome: FoldOutcome
    state: AgentSessionState
    disposition: AgentSessionDisposition | None
    reason: str


def fold_agent_session_state(
    *, current: AgentSessionState, proposed: AgentSessionState
) -> SessionFoldResult:
    """Decide how a proposed agent-session state folds into the current state.

    A terminal state is absorbing. A terminal proposal always wins from any live state.
    A live -> live transition applies only when it is a declared edge in the interaction
    graph; anything else is ignored (deterministic no-op)."""
    if current in _SESSION_TERMINAL:
        reason = "duplicate-terminal" if proposed == current else "session-already-terminal"
        return SessionFoldResult(
            FoldOutcome.IGNORED, current, _SESSION_DISPOSITION_FOR.get(current), reason
        )

    if proposed == current:
        return SessionFoldResult(FoldOutcome.IGNORED, current, None, "duplicate-state")

    if proposed in _SESSION_TERMINAL:
        return SessionFoldResult(
            FoldOutcome.APPLIED,
            proposed,
            _SESSION_DISPOSITION_FOR[proposed],
            "terminal-transition",
        )

    if proposed in _SESSION_EDGES.get(current, frozenset()):
        return SessionFoldResult(FoldOutcome.APPLIED, proposed, None, "live-transition")

    return SessionFoldResult(FoldOutcome.IGNORED, current, None, "illegal-live-transition")


@dataclass(frozen=True, slots=True)
class TurnFoldResult:
    outcome: FoldOutcome
    state: AgentTurnState
    reason: str


def fold_agent_turn_state(*, current: AgentTurnState, proposed: AgentTurnState) -> TurnFoldResult:
    if current in _TURN_TERMINAL:
        reason = "duplicate-terminal" if proposed == current else "turn-already-terminal"
        return TurnFoldResult(FoldOutcome.IGNORED, current, reason)
    if proposed == current:
        return TurnFoldResult(FoldOutcome.IGNORED, current, "duplicate-state")
    if proposed in _TURN_TERMINAL:
        return TurnFoldResult(FoldOutcome.APPLIED, proposed, "terminal-transition")
    if proposed in _TURN_EDGES.get(current, frozenset()):
        return TurnFoldResult(FoldOutcome.APPLIED, proposed, "live-transition")
    return TurnFoldResult(FoldOutcome.IGNORED, current, "illegal-live-transition")
