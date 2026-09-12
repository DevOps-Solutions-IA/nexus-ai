"""agent turn execution-lease primitives (NXS-P13: NXS-AGENT-001, audit corrective #6)

Adds two durable, per-attempt facts to ``ai_agent_turns`` — NOT used by P13 to implement
autonomous crash recovery (see ADR-0094 "Orphaned RUNNING turn recovery" and requirement
``NXS-AGENT-002``, target phase NXS-P25), but the minimum foundation a future recovery
mechanism needs to distinguish an orphaned RUNNING turn (its claiming worker crashed or was
partitioned) from a genuinely live one, WITHOUT ambiguity:

* ``execution_owner_id`` — a random token minted once, at turn-claim time, identifying
  THIS execution attempt. Never reused, never renewed, never compared for correctness by
  P13 itself (a turn is already claimed exclusively by exactly one worker at the DATABASE
  boundary — the partial UNIQUE index on the idempotency key / the ``active_for_session``
  admission check — this column does not change that). It exists so a future reaper can
  label / log / correlate which attempt it is recovering from, and so a later P13
  extension (if ever needed) has a concrete token to fence on rather than inventing one
  post hoc against historical rows that never had one.
* ``lease_expires_at`` — set once, at turn-claim time, to ``now() + min(the global turn
  deadline ceiling, the session's remaining absolute lifetime) + execution_lease_grace_seconds``.
  This is a conservative UPPER BOUND on how long a LEGITIMATE worker could still be running
  this turn: ``submit_turn`` wraps the entire model+tool loop in ``asyncio.timeout(deadline)``
  using that same bound (a tenant's tighter ``agent.timeout_seconds`` can only shrink the
  real deadline further, never widen it — so this column may slightly OVERESTIMATE a live
  worker's remaining time, which only delays safe-reap eligibility, it never shortens it
  incorrectly). A RUNNING turn whose ``lease_expires_at`` has passed is therefore an
  UNAMBIGUOUS signal: no legitimately-alive worker can still be executing it, because its
  own ``asyncio.timeout`` would already have fired and driven it to a terminal state if it
  were still alive. P13 never reads this column to make a decision — it is a pure durable
  fact for NXS-P25 (Resilience).

Both columns are NULLABLE and NOT backfilled: a pre-existing turn (created before this
migration) never had an owner token or a lease, and none can be honestly reconstructed for
it. Such a row's ``lease_expires_at IS NULL`` must be treated by any future reaper as
"unknown deadline — do not assume safe to reap", never as "safe to reap" nor as "never
expires" — this asymmetry is deliberate and documented, matching the same
backfill/non-backfill discipline used for ``response_correlation_id`` in migration
``d0e1f2a3b4c5``.

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-09-11 12:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PgUUID

revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "d0e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ai_agent_turns",
        sa.Column("execution_owner_id", PgUUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "ai_agent_turns",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ai_agent_turns", "lease_expires_at")
    op.drop_column("ai_agent_turns", "execution_owner_id")
