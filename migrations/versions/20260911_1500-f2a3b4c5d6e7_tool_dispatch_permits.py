"""agent tool-dispatch permits — the linearization point for new P08 execution
(NXS-P13: NXS-AGENT-001, audit corrective #7)

Adds ``ai_agent_tool_dispatch_permits``: a durable, append-only, tenant-owned record of
every NEW semantic tool call a turn was ever AUTHORIZED to dispatch to the NXS-P08 Tool
Engine. This table IS the linearization point corrective #7 requires — it does not
replace or duplicate ``ai_agent_tool_calls`` (which continues to record the OUTCOME of an
authorized call, exactly as before; a permit is never itself a ``ToolExecutionRecord``).

Why this closes the TOCTOU corrective #6 could not: a permit is inserted inside the SAME
transaction that takes ``SELECT … FOR UPDATE`` on the owning ``ai_agent_sessions`` row —
the identical row lock ``AgentService._terminalize`` (stop / cancel / expire) takes. Two
transactions contending for the same row lock are serialized by PostgreSQL itself: the
one that acquires the lock first determines what the other observes. A permit-insert
transaction that wins the lock race observes the session ACTIVE, persists the permit and
commits BEFORE a concurrently-committing cancellation is even attempted — the operation is
now durably, provably AUTHORIZED, and may complete even if cancellation commits a moment
later. A permit-insert transaction that loses the race (cancellation's commit already
released the lock) observes the session already terminal and is rejected — no permit row
is ever created, so the external dispatch it would have guarded never begins. This is an
objective, PostgreSQL-enforced happens-before relationship, not a probabilistic narrowing
of a check-then-act window.

The unique index on ``(organization_id, turn_id, tool_key, arguments_hash)`` is
defense-in-depth (a turn is already exclusively owned by one worker — correctives #1/#4 —
so two concurrent authorization attempts for the identical semantic call should not be
reachable in practice); it guarantees INV-FENCE-011 (fencing never creates a duplicate
``ToolExecutionRecord``) cannot be violated even if that assumption is ever wrong.

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-09-11 15:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from nexus_ai.infrastructure.rls import apply_tenant_rls, drop_tenant_rls

revision: str = "f2a3b4c5d6e7"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.text("now()")


def _org_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id"],
        ["organizations.id"],
        name=op.f(f"fk_{table}_organization_id_organizations"),
        ondelete="RESTRICT",
    )


def upgrade() -> None:
    op.create_table(
        "ai_agent_tool_dispatch_permits",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("turn_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("tool_key", sa.String(length=96), nullable=False),
        sa.Column("arguments_hash", sa.String(length=64), nullable=False),
        sa.Column("authorized_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "turn_id"],
            ["ai_agent_turns.organization_id", "ai_agent_turns.id"],
            name="fk_ai_agent_tool_dispatch_permits_org_turn",
            ondelete="CASCADE",
        ),
        _org_fk("ai_agent_tool_dispatch_permits"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_agent_tool_dispatch_permits")),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_ai_agent_tool_dispatch_permits_org_id"
        ),
    )
    op.create_index(
        "ix_ai_agent_tool_dispatch_permits_organization_id",
        "ai_agent_tool_dispatch_permits",
        ["organization_id"],
    )
    op.create_index(
        "ix_ai_agent_tool_dispatch_permits_turn",
        "ai_agent_tool_dispatch_permits",
        ["organization_id", "turn_id"],
    )
    op.create_index(
        "uq_ai_agent_tool_dispatch_permits_semantic",
        "ai_agent_tool_dispatch_permits",
        ["organization_id", "turn_id", "tool_key", "arguments_hash"],
        unique=True,
    )
    apply_tenant_rls(op, "ai_agent_tool_dispatch_permits")


def downgrade() -> None:
    drop_tenant_rls(op, "ai_agent_tool_dispatch_permits")
    op.drop_index(
        "uq_ai_agent_tool_dispatch_permits_semantic",
        table_name="ai_agent_tool_dispatch_permits",
    )
    op.drop_index(
        "ix_ai_agent_tool_dispatch_permits_turn", table_name="ai_agent_tool_dispatch_permits"
    )
    op.drop_index(
        "ix_ai_agent_tool_dispatch_permits_organization_id",
        table_name="ai_agent_tool_dispatch_permits",
    )
    op.drop_table("ai_agent_tool_dispatch_permits")
