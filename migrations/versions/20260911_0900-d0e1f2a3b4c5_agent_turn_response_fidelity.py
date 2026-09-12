"""agent turn response-fidelity facts (NXS-P13: NXS-AGENT-001, audit corrective #5)

Adds two immutable facts to ``ai_agent_turns`` so a REPLAYED ``AgentResponse`` is
externally equivalent to the ORIGINAL one:

* ``tool_call_count`` — the number of DISTINCT tool calls actually executed this turn
  (``len(outcome.tool_calls)`` at completion time — never the loop-iteration count
  ``tool_iterations``, which is a different number whenever one model response requests
  more than one tool). BACKFILLED for every pre-existing turn that has at least one row in
  ``ai_agent_tool_calls`` (a turn with none keeps the column default of 0): one row exists
  per DISTINCT executed call (the ``on_tool`` callback fires only on the runtime's
  cache-miss branch — see ``nexus_ai.agents.runtime``), so ``count(*)`` over that table per
  ``turn_id`` is exactly the historical fact this column is meant to hold, whatever the
  turn's terminal state (only a ``COMPLETED`` turn's count is ever read by a replay).
* ``response_correlation_id`` — the correlation id the ORIGINAL response was published
  under (``ctx_correlation(request, session)``), so a replay never silently returns
  ``correlation_id: null``. This fact was NEVER persisted anywhere before this migration,
  so it cannot be backfilled for a pre-existing turn; such a turn's replay will report
  ``correlation_id: null`` exactly as it did before this corrective (a non-regression, not
  a new defect — there is no historical value to recover).

Both are persisted once, at ``_finish_turn`` completion time going forward, and never
change again — matching "one idempotency key names one immutable logical turn".

Revision ID: d0e1f2a3b4c5
Revises: c9e0f1a2b3c4
Create Date: 2026-09-11 09:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d0e1f2a3b4c5"
down_revision: str | None = "c9e0f1a2b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ai_agent_turns",
        sa.Column("tool_call_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "ai_agent_turns",
        sa.Column("response_correlation_id", sa.String(length=128), nullable=True),
    )
    # Backfill: for any turn that completed under the pre-corrective code, derive its
    # historical tool_call_count from the durable ai_agent_tool_calls rows rather than
    # leaving it at the column default (which would make a replay of an OLD turn report
    # tool_calls=0 even when tools actually ran).
    op.execute(
        """
        UPDATE ai_agent_turns AS t
        SET tool_call_count = c.n
        FROM (
            SELECT turn_id, count(*) AS n
            FROM ai_agent_tool_calls
            GROUP BY turn_id
        ) AS c
        WHERE t.id = c.turn_id
        """
    )


def downgrade() -> None:
    op.drop_column("ai_agent_turns", "response_correlation_id")
    op.drop_column("ai_agent_turns", "tool_call_count")
