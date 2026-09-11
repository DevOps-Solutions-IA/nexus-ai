"""agent model-dispatch permits — the linearization point for new provider invocation
(NXS-P13: NXS-AGENT-001, audit corrective #8)

Adds ``ai_agent_model_dispatch_permits``: the model-invocation counterpart to corrective
#7's ``ai_agent_tool_dispatch_permits``. Together they give BOTH classes of external work
P13 can dispatch (a model-provider call, a NXS-P08 tool call) an identical durable
linearization point against cross-worker cancellation — closing the residual TOCTOU
window corrective #6's plain, unlocked ``_check_execution_authority`` read left open for
model continuations (it was only ever closed for tool dispatch, by corrective #7).

Same mechanism, same proof: a permit is inserted inside the SAME transaction that takes
``SELECT … FOR UPDATE`` on the owning ``ai_agent_sessions`` row — the identical lock
``AgentService._terminalize`` (stop / cancel / expire) takes. Two transactions contending
for the same row lock are serialized by PostgreSQL itself, making "did this model call's
authorization happen before or after the cancellation" an objective, durable fact.

Deliberately does NOT store the prompt, the assembled context, provider credentials, or
any hidden reasoning — only enough to prove a specific iteration of a specific turn was
authorized to call the provider: ``(organization_id, session_id, turn_id, iteration,
model)``. ``model`` is the resolved model identifier string (e.g. ``"gpt-4o-mini"``), not
provider-account or credential material.

The unique index on ``(organization_id, turn_id, iteration)`` is defense-in-depth (a turn
is already exclusively owned by one worker — correctives #1/#4 — so two concurrent
authorization attempts for the identical iteration should not be reachable in practice);
it guarantees INV-EXEC-012 (no permit mechanism duplicates usage accounting) cannot be
violated even if that assumption is ever wrong.

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-09-11 18:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from nexus_ai.infrastructure.rls import apply_tenant_rls, drop_tenant_rls

revision: str = "a3b4c5d6e7f8"
down_revision: str | None = "f2a3b4c5d6e7"
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
        "ai_agent_model_dispatch_permits",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("turn_id", sa.UUID(), nullable=False),
        sa.Column("iteration", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(length=96), nullable=False),
        sa.Column("authorized_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "turn_id"],
            ["ai_agent_turns.organization_id", "ai_agent_turns.id"],
            name="fk_ai_agent_model_dispatch_permits_org_turn",
            ondelete="CASCADE",
        ),
        _org_fk("ai_agent_model_dispatch_permits"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_agent_model_dispatch_permits")),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_ai_agent_model_dispatch_permits_org_id"
        ),
    )
    op.create_index(
        "ix_ai_agent_model_dispatch_permits_organization_id",
        "ai_agent_model_dispatch_permits",
        ["organization_id"],
    )
    op.create_index(
        "ix_ai_agent_model_dispatch_permits_turn",
        "ai_agent_model_dispatch_permits",
        ["organization_id", "turn_id"],
    )
    op.create_index(
        "uq_ai_agent_model_dispatch_permits_iteration",
        "ai_agent_model_dispatch_permits",
        ["organization_id", "turn_id", "iteration"],
        unique=True,
    )
    apply_tenant_rls(op, "ai_agent_model_dispatch_permits")


def downgrade() -> None:
    drop_tenant_rls(op, "ai_agent_model_dispatch_permits")
    op.drop_index(
        "uq_ai_agent_model_dispatch_permits_iteration",
        table_name="ai_agent_model_dispatch_permits",
    )
    op.drop_index(
        "ix_ai_agent_model_dispatch_permits_turn", table_name="ai_agent_model_dispatch_permits"
    )
    op.drop_index(
        "ix_ai_agent_model_dispatch_permits_organization_id",
        table_name="ai_agent_model_dispatch_permits",
    )
    op.drop_table("ai_agent_model_dispatch_permits")
