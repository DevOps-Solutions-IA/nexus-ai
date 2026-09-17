"""durable tenant-isolated Human Agent Operations (NXS-P17)

Revision ID: f17a0b1c2d3e
Revises: b0c1d2e3f4a5
Create Date: 2026-09-17 12:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from nexus_ai.domain.auth.rbac import PERMISSION_IDS, ROLE_IDS, PermissionKey, RoleKey
from nexus_ai.infrastructure.rls import apply_tenant_rls, drop_tenant_rls

revision: str = "f17a0b1c2d3e"
down_revision: str | None = "b0c1d2e3f4a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSONB = postgresql.JSONB(astext_type=sa.Text())
_NOW = sa.text("now()")
_PERMISSIONS = (
    (PermissionKey.HUMAN_READ, "Read human queues, presence, work, assignments and history"),
    (PermissionKey.HUMAN_WORK, "Claim and operate Organization-scoped human work"),
    (PermissionKey.HUMAN_CONFIGURE, "Configure human queues and routing policy"),
    (PermissionKey.HUMAN_SUPERVISE, "Release, requeue and transfer human work as supervisor"),
)


def _tenant_table(name: str, *items: object) -> None:
    op.create_table(
        name,
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        *items,
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f(f"fk_{name}_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{name}")),
    )
    op.create_index(op.f(f"ix_{name}_organization_id"), name, ["organization_id"])
    apply_tenant_rls(op, name)


def upgrade() -> None:
    _tenant_table(
        "human_queues",
        sa.Column("queue_key", sa.String(64), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("supported_channels", _JSONB, nullable=False),
        sa.Column("required_skills", _JSONB, nullable=False),
        sa.Column("max_active_assignments", sa.Integer(), nullable=False),
        sa.Column("sla_target_seconds", sa.Integer(), nullable=True),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint("organization_id", "id", name="uq_human_queues_org_id"),
        sa.UniqueConstraint("organization_id", "queue_key", name="uq_human_queues_org_key"),
        sa.CheckConstraint(
            "max_active_assignments BETWEEN 1 AND 100", name="ck_human_queues_capacity_bounds"
        ),
        sa.CheckConstraint("revision >= 1", name="ck_human_queues_revision_positive"),
    )
    _tenant_table(
        "human_agent_presence",
        sa.Column("agent_user_id", sa.UUID(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("active_assignment_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint("organization_id", "id", name="uq_human_presence_org_id"),
        sa.UniqueConstraint("organization_id", "agent_user_id", name="uq_human_presence_org_agent"),
        sa.ForeignKeyConstraint(
            ["organization_id", "agent_user_id"],
            ["memberships.organization_id", "memberships.user_id"],
            name="fk_human_presence_org_membership",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "state IN ('OFFLINE','AVAILABLE','BUSY','AWAY','WRAP_UP')",
            name="ck_human_agent_presence_state_known",
        ),
        sa.CheckConstraint(
            "capacity BETWEEN 0 AND 100", name="ck_human_agent_presence_capacity_bounds"
        ),
        sa.CheckConstraint(
            "active_assignment_count BETWEEN 0 AND capacity",
            name="ck_human_agent_presence_active_count_bounds",
        ),
        sa.CheckConstraint("version >= 1", name="ck_human_agent_presence_version_positive"),
    )
    _tenant_table(
        "human_work_items",
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=True),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=True),
        sa.Column("call_id", sa.UUID(), nullable=True),
        sa.Column("voice_session_id", sa.UUID(), nullable=True),
        sa.Column("queue_id", sa.UUID(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("handoff_reason", sa.String(96), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("eligible_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("assigned_agent_id", sa.UUID(), nullable=True),
        sa.Column("current_assignment_id", sa.UUID(), nullable=True),
        sa.Column("lease_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_claim_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_response_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint("organization_id", "id", name="uq_human_work_items_org_id"),
        sa.UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_human_work_items_idempotency"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["conversations.organization_id", "conversations.id"],
            name="fk_human_work_org_conversation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_human_work_org_customer",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "queue_id"],
            ["human_queues.organization_id", "human_queues.id"],
            name="fk_human_work_org_queue",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "call_id"],
            ["telephony_calls.organization_id", "telephony_calls.id"],
            name="fk_human_work_org_call",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "voice_session_id"],
            ["voice_sessions.organization_id", "voice_sessions.id"],
            name="fk_human_work_org_voice_session",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "assigned_agent_id"],
            ["memberships.organization_id", "memberships.user_id"],
            name="fk_human_work_org_assigned_agent",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "state IN ('QUEUED','CLAIMED','ACCEPTED','ACTIVE','AI_RETURN_PENDING',"
            "'WRAP_UP','COMPLETED','CANCELLED')",
            name="ck_human_work_items_state_known",
        ),
        sa.CheckConstraint(
            "priority BETWEEN -1000 AND 1000", name="ck_human_work_items_priority_bounds"
        ),
        sa.CheckConstraint(
            "lease_version >= 0", name="ck_human_work_items_lease_version_nonnegative"
        ),
    )
    op.create_index(
        "ix_human_work_routing",
        "human_work_items",
        [
            "organization_id",
            "queue_id",
            "state",
            sa.text("priority DESC"),
            "eligible_at",
            "created_at",
            "id",
        ],
    )
    _tenant_table(
        "conversation_ownership",
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("ownership_generation", sa.Integer(), server_default="1", nullable=False),
        sa.Column("work_item_id", sa.UUID(), nullable=True),
        sa.Column("assignment_id", sa.UUID(), nullable=True),
        sa.Column("agent_user_id", sa.UUID(), nullable=True),
        sa.Column("ai_session_id", sa.UUID(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint("organization_id", "id", name="uq_conversation_ownership_org_id"),
        sa.UniqueConstraint(
            "organization_id", "conversation_id", name="uq_conversation_ownership_org_conversation"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["conversations.organization_id", "conversations.id"],
            name="fk_conversation_ownership_org_conversation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "agent_user_id"],
            ["memberships.organization_id", "memberships.user_id"],
            name="fk_conversation_ownership_org_agent",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "ai_session_id"],
            ["ai_agent_sessions.organization_id", "ai_agent_sessions.id"],
            name="fk_conversation_ownership_org_ai_session",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "mode IN ('AI','HUMAN','UNASSIGNED')", name="ck_conversation_ownership_mode_known"
        ),
        sa.CheckConstraint(
            "ownership_generation >= 1", name="ck_conversation_ownership_generation_positive"
        ),
    )
    _tenant_table(
        "human_assignments",
        sa.Column("work_item_id", sa.UUID(), nullable=False),
        sa.Column("queue_id", sa.UUID(), nullable=False),
        sa.Column("owner_agent_id", sa.UUID(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("claim_token_hash", sa.String(64), nullable=False),
        sa.Column("lease_version", sa.Integer(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_reason", sa.String(96), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint("organization_id", "id", name="uq_human_assignments_org_id"),
        sa.ForeignKeyConstraint(
            ["organization_id", "work_item_id"],
            ["human_work_items.organization_id", "human_work_items.id"],
            name="fk_human_assignments_org_work",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "queue_id"],
            ["human_queues.organization_id", "human_queues.id"],
            name="fk_human_assignments_org_queue",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "owner_agent_id"],
            ["memberships.organization_id", "memberships.user_id"],
            name="fk_human_assignments_org_agent",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "state IN ('CLAIMED','ACCEPTED','ACTIVE','RELEASED','TRANSFERRED','COMPLETED')",
            name="ck_human_assignments_state_known",
        ),
        sa.CheckConstraint("lease_version >= 1", name="ck_human_assignments_lease_positive"),
    )
    op.create_index(
        "uq_human_assignments_active_work",
        "human_assignments",
        ["organization_id", "work_item_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('CLAIMED','ACCEPTED','ACTIVE')"),
    )
    op.create_foreign_key(
        "fk_human_work_org_current_assignment",
        "human_work_items",
        "human_assignments",
        ["organization_id", "current_assignment_id"],
        ["organization_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_conversation_ownership_org_work",
        "conversation_ownership",
        "human_work_items",
        ["organization_id", "work_item_id"],
        ["organization_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_conversation_ownership_org_assignment",
        "conversation_ownership",
        "human_assignments",
        ["organization_id", "assignment_id"],
        ["organization_id", "id"],
        ondelete="RESTRICT",
    )
    _tenant_table(
        "human_handoffs",
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("work_item_id", sa.UUID(), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("semantic_fingerprint", sa.String(64), nullable=False),
        sa.Column("ownership_generation", sa.Integer(), nullable=False),
        sa.Column("p13_idempotency_key", sa.String(200), nullable=True),
        sa.Column("p13_session_id", sa.UUID(), nullable=True),
        sa.Column("error_code", sa.String(96), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint("organization_id", "id", name="uq_human_handoffs_org_id"),
        sa.UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_human_handoffs_idempotency"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["conversations.organization_id", "conversations.id"],
            name="fk_human_handoffs_org_conversation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "work_item_id"],
            ["human_work_items.organization_id", "human_work_items.id"],
            name="fk_human_handoffs_org_work",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "p13_session_id"],
            ["ai_agent_sessions.organization_id", "ai_agent_sessions.id"],
            name="fk_human_handoffs_org_p13_session",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "direction IN ('AI_TO_HUMAN','HUMAN_TO_AI')", name="ck_human_handoffs_direction_known"
        ),
        sa.CheckConstraint(
            "state IN ('REQUESTED','AI_RETURN_PENDING','ACCEPTED','P13_REJECTED',"
            "'AMBIGUOUS','COMPLETED')",
            name="ck_human_handoffs_state_known",
        ),
    )
    _tenant_table(
        "human_action_authorizations",
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("work_item_id", sa.UUID(), nullable=False),
        sa.Column("assignment_id", sa.UUID(), nullable=False),
        sa.Column("lease_version", sa.Integer(), nullable=False),
        sa.Column("ownership_generation", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("semantic_fingerprint", sa.String(64), nullable=False),
        sa.Column("p09_idempotency_key", sa.String(200), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("message_id", sa.UUID(), nullable=True),
        sa.Column("error_code", sa.String(96), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint("organization_id", "id", name="uq_human_action_auth_org_id"),
        sa.UniqueConstraint(
            "organization_id", "p09_idempotency_key", name="uq_human_action_auth_p09_key"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["conversations.organization_id", "conversations.id"],
            name="fk_human_action_auth_org_conversation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "work_item_id"],
            ["human_work_items.organization_id", "human_work_items.id"],
            name="fk_human_action_auth_org_work",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "assignment_id"],
            ["human_assignments.organization_id", "human_assignments.id"],
            name="fk_human_action_auth_org_assignment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "message_id"],
            ["messaging_messages.organization_id", "messaging_messages.id"],
            name="fk_human_action_auth_org_message",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "state IN ('AUTHORIZED','CONSUMED','FAILED','AMBIGUOUS')",
            name="ck_human_action_authorizations_state_known",
        ),
    )
    _tenant_table(
        "human_transition_history",
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column("previous_state", sa.String(24), nullable=True),
        sa.Column("new_state", sa.String(24), nullable=False),
        sa.Column("reason_code", sa.String(96), nullable=False),
        sa.Column("correlation_id", sa.String(128), nullable=True),
        sa.Column("metadata_json", _JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint("organization_id", "id", name="uq_human_history_org_id"),
    )
    op.create_index(
        "ix_human_history_entity",
        "human_transition_history",
        ["organization_id", "entity_type", "entity_id", "created_at"],
    )
    op.execute("""
      CREATE FUNCTION nxs_guard_human_history() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'human transition history is immutable' USING ERRCODE='55000'; END; $$
    """)
    op.execute(
        "CREATE TRIGGER nxs_immutable_human_history BEFORE UPDATE OR DELETE "
        "ON human_transition_history FOR EACH ROW "
        "EXECUTE FUNCTION nxs_guard_human_history()"
    )
    _seed_permissions()


def _seed_permissions() -> None:
    permissions = sa.table(
        "permissions",
        sa.column("id", sa.UUID()),
        sa.column("permission_key", sa.String()),
        sa.column("description", sa.String()),
    )
    op.bulk_insert(
        permissions,
        [
            {"id": PERMISSION_IDS[key], "permission_key": key.value, "description": description}
            for key, description in _PERMISSIONS
        ],
    )
    grants = sa.table(
        "role_permissions", sa.column("role_id", sa.UUID()), sa.column("permission_id", sa.UUID())
    )
    rows = [
        {"role_id": ROLE_IDS[role], "permission_id": PERMISSION_IDS[key]}
        for role in (RoleKey.ORG_OWNER, RoleKey.ORG_ADMIN)
        for key, _ in _PERMISSIONS
    ]
    rows += [
        {"role_id": ROLE_IDS[RoleKey.ORG_MEMBER], "permission_id": PERMISSION_IDS[key]}
        for key in (PermissionKey.HUMAN_READ, PermissionKey.HUMAN_WORK)
    ]
    op.bulk_insert(grants, rows)


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "('b2000000-0000-7000-8000-000000000039',"
        "'b2000000-0000-7000-8000-00000000003a',"
        "'b2000000-0000-7000-8000-00000000003b',"
        "'b2000000-0000-7000-8000-00000000003c')"
    )
    op.execute("DELETE FROM permissions WHERE permission_key LIKE 'human:%'")
    op.execute("DROP TRIGGER IF EXISTS nxs_immutable_human_history ON human_transition_history")
    op.execute("DROP FUNCTION IF EXISTS nxs_guard_human_history()")
    for table in (
        "human_transition_history",
        "human_action_authorizations",
        "human_handoffs",
    ):
        drop_tenant_rls(op, table)
        op.drop_table(table)
    op.drop_constraint(
        "fk_conversation_ownership_org_assignment", "conversation_ownership", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_conversation_ownership_org_work", "conversation_ownership", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_human_work_org_current_assignment", "human_work_items", type_="foreignkey"
    )
    for table in (
        "human_assignments",
        "conversation_ownership",
        "human_work_items",
        "human_agent_presence",
        "human_queues",
    ):
        drop_tenant_rls(op, table)
        op.drop_table(table)
