"""agent session EXPIRED terminal state (NXS-P13: NXS-AGENT-001, audit corrective #2)

Adds ``'EXPIRED'`` to the ``ai_agent_sessions.state`` domain. An agent session that
reaches its absolute lifetime ceiling (``started_at + max_session_seconds``) is
terminalised ``EXPIRED`` — a truthful terminal distinct from success (``COMPLETED``),
error (``FAILED``) and barge-in / client cancellation (``CANCELLED``). ``state_rank``
already permits 0..4 and ``EXPIRED`` ranks 4, so only the state CHECK changes.

Revision ID: c9e0f1a2b3c4
Revises: b7c8d9e0f1a2
Create Date: 2026-09-10 19:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c9e0f1a2b3c4"
down_revision: str | None = "b7c8d9e0f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_ai_agent_sessions_state_known"
_WITH_EXPIRED = (
    "state IN ('PENDING','ACTIVE','WAITING_TOOL','RESPONDING',"
    "'COMPLETED','FAILED','CANCELLED','EXPIRED')"
)
_WITHOUT_EXPIRED = (
    "state IN ('PENDING','ACTIVE','WAITING_TOOL','RESPONDING','COMPLETED','FAILED','CANCELLED')"
)


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "ai_agent_sessions", type_="check")
    op.create_check_constraint(_CONSTRAINT, "ai_agent_sessions", _WITH_EXPIRED)


def downgrade() -> None:
    op.execute("UPDATE ai_agent_sessions SET state = 'CANCELLED' WHERE state = 'EXPIRED'")
    op.drop_constraint(_CONSTRAINT, "ai_agent_sessions", type_="check")
    op.create_check_constraint(_CONSTRAINT, "ai_agent_sessions", _WITHOUT_EXPIRED)
