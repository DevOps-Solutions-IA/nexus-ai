"""agent turn/session composite identity — database-level authority-identity backstop
(NXS-P13: NXS-AGENT-001, audit corrective #9)

Correctives #7/#8 gave tool- and model-dispatch authorization a durable, PostgreSQL
-provable LINEARIZATION POINT (a ``SELECT ... FOR UPDATE`` on the owning
``ai_agent_sessions`` row, in the same transaction as a durable permit insert). Neither
corrective proved the session row being locked was actually the AUTHORIZED TURN's own
parent session — ``AgentService._assert_turn_authority`` checked turn-terminality and
``execution_owner_id`` but never ``turn.session_id == session_id``. Within one
Organization, RLS does not help: a caller-supplied session and a turn's real parent
session belong to the same tenant, so a tenant-scoped read of either succeeds regardless
of whether they are actually related to each other.

The corrective #9 application-level fix (an explicit ``turn.session_id ==
expected_session_id`` check inside ``_assert_turn_authority``) is necessary but, per
directive §5, insufficient on its own: "Do NOT rely only on Python." This migration is
the DATABASE-level backstop:

* ``ai_agent_turns`` gains a SECOND, additive unique constraint on
  ``(organization_id, session_id, id)`` — alongside the existing
  ``(organization_id, id)`` constraint, which is PRESERVED (nothing about this migration
  requires removing it, and other code may still depend on it; removing a working
  constraint for no functional gain is pure unnecessary risk).
* ``ai_agent_tool_dispatch_permits``, ``ai_agent_model_dispatch_permits`` and
  ``ai_agent_tool_calls`` all move their FK on ``turn_id`` from the plain
  ``(organization_id, turn_id) -> ai_agent_turns(organization_id, id)`` to the composite
  ``(organization_id, session_id, turn_id) -> ai_agent_turns(organization_id, session_id,
  id)``. After this migration, PostgreSQL itself — not just application code — refuses to
  persist a row in any of these three tables whose ``session_id`` disagrees with its own
  ``turn_id``'s actual parent session. ``ai_agent_tool_calls`` is included even though it
  is not itself an authorization gate: it is the durable AUDIT RECORD of an authorized
  dispatch's effect, and leaving it on the old, non-session-aware FK while the permit
  tables that authorize the very same dispatch move to the session-aware key would be a
  silent relational contradiction between "what was authorized" and "what was recorded"
  (directive corrective #9 §6).
* The three tables' existing ``ix_*_turn`` lookup indexes (``(organization_id,
  turn_id)``) are recreated as ``(organization_id, session_id, turn_id)`` — the SAME
  column order as their new composite FK — so the FK's own cascade-delete lookup, and
  any query filtering on the full triple, can use the index directly rather than a
  partial match (independent-audit LOW finding, addressed before closure).

No other table currently has a foreign key on ``ai_agent_turns(organization_id, id)`` —
verified directly against the ORM (``grep`` over every ``ForeignKeyConstraint`` in
``domain/agents/models.py``) before writing this migration.

Data safety: this repository's own governance state (``.nxs/project-state.json`` /
``.nxs/phase-registry.json`` and every corrective in this branch's lineage) records
NXS-P13 as NEVER merged to ``main`` and NEVER deployed — PR #24 remains open, and every
prior corrective's report on this branch explicitly ends "MERGE NOT AUTHORIZED. DEPLOY
NOT AUTHORIZED." All three child tables were themselves created BY correctives #7/#8 on
this SAME unmerged branch (migrations ``f2a3b4c5d6e7`` and ``a3b4c5d6e7f8``); there is no
production data this migration could encounter, checked against repository governance
rather than assumed. No destructive data rewrite is performed regardless: this migration
adds constraints, it does not rewrite or delete any row.

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-09-12 09:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b4c5d6e7f8a9"
down_revision: str | None = "a3b4c5d6e7f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1) the composite unique key every child FK below will reference. Additive: the
    #    existing (organization_id, id) constraint is untouched.
    op.create_unique_constraint(
        "uq_ai_agent_turns_org_session_id",
        "ai_agent_turns",
        ["organization_id", "session_id", "id"],
    )

    # 2) ai_agent_tool_dispatch_permits: swap the plain org+turn FK for the composite
    #    org+session+turn FK.
    op.drop_constraint(
        "fk_ai_agent_tool_dispatch_permits_org_turn",
        "ai_agent_tool_dispatch_permits",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_ai_agent_tool_dispatch_permits_org_session_turn",
        "ai_agent_tool_dispatch_permits",
        "ai_agent_turns",
        ["organization_id", "session_id", "turn_id"],
        ["organization_id", "session_id", "id"],
        ondelete="CASCADE",
    )
    op.drop_index(
        "ix_ai_agent_tool_dispatch_permits_turn", table_name="ai_agent_tool_dispatch_permits"
    )
    op.create_index(
        "ix_ai_agent_tool_dispatch_permits_turn",
        "ai_agent_tool_dispatch_permits",
        ["organization_id", "session_id", "turn_id"],
    )

    # 3) ai_agent_model_dispatch_permits: identical swap.
    op.drop_constraint(
        "fk_ai_agent_model_dispatch_permits_org_turn",
        "ai_agent_model_dispatch_permits",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_ai_agent_model_dispatch_permits_org_session_turn",
        "ai_agent_model_dispatch_permits",
        "ai_agent_turns",
        ["organization_id", "session_id", "turn_id"],
        ["organization_id", "session_id", "id"],
        ondelete="CASCADE",
    )
    op.drop_index(
        "ix_ai_agent_model_dispatch_permits_turn", table_name="ai_agent_model_dispatch_permits"
    )
    op.create_index(
        "ix_ai_agent_model_dispatch_permits_turn",
        "ai_agent_model_dispatch_permits",
        ["organization_id", "session_id", "turn_id"],
    )

    # 4) ai_agent_tool_calls: identical swap — the audit-record table, not an
    #    authorization gate, but session-identity-consistent for the same reason.
    op.drop_constraint(
        "fk_ai_agent_tool_calls_org_turn",
        "ai_agent_tool_calls",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_ai_agent_tool_calls_org_session_turn",
        "ai_agent_tool_calls",
        "ai_agent_turns",
        ["organization_id", "session_id", "turn_id"],
        ["organization_id", "session_id", "id"],
        ondelete="CASCADE",
    )
    op.drop_index("ix_ai_agent_tool_calls_turn", table_name="ai_agent_tool_calls")
    op.create_index(
        "ix_ai_agent_tool_calls_turn",
        "ai_agent_tool_calls",
        ["organization_id", "session_id", "turn_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_agent_tool_calls_turn", table_name="ai_agent_tool_calls")
    op.create_index(
        "ix_ai_agent_tool_calls_turn", "ai_agent_tool_calls", ["organization_id", "turn_id"]
    )
    op.drop_constraint(
        "fk_ai_agent_tool_calls_org_session_turn",
        "ai_agent_tool_calls",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_ai_agent_tool_calls_org_turn",
        "ai_agent_tool_calls",
        "ai_agent_turns",
        ["organization_id", "turn_id"],
        ["organization_id", "id"],
        ondelete="CASCADE",
    )

    op.drop_index(
        "ix_ai_agent_model_dispatch_permits_turn", table_name="ai_agent_model_dispatch_permits"
    )
    op.create_index(
        "ix_ai_agent_model_dispatch_permits_turn",
        "ai_agent_model_dispatch_permits",
        ["organization_id", "turn_id"],
    )
    op.drop_constraint(
        "fk_ai_agent_model_dispatch_permits_org_session_turn",
        "ai_agent_model_dispatch_permits",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_ai_agent_model_dispatch_permits_org_turn",
        "ai_agent_model_dispatch_permits",
        "ai_agent_turns",
        ["organization_id", "turn_id"],
        ["organization_id", "id"],
        ondelete="CASCADE",
    )

    op.drop_index(
        "ix_ai_agent_tool_dispatch_permits_turn", table_name="ai_agent_tool_dispatch_permits"
    )
    op.create_index(
        "ix_ai_agent_tool_dispatch_permits_turn",
        "ai_agent_tool_dispatch_permits",
        ["organization_id", "turn_id"],
    )
    op.drop_constraint(
        "fk_ai_agent_tool_dispatch_permits_org_session_turn",
        "ai_agent_tool_dispatch_permits",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_ai_agent_tool_dispatch_permits_org_turn",
        "ai_agent_tool_dispatch_permits",
        "ai_agent_turns",
        ["organization_id", "turn_id"],
        ["organization_id", "id"],
        ondelete="CASCADE",
    )

    op.drop_constraint("uq_ai_agent_turns_org_session_id", "ai_agent_turns", type_="unique")
