"""agent runtime (NXS-P13: NXS-AGENT-001)

Eight TENANT-OWNED tables with forced RLS for the AI Agent Runtime. An
``ai_agent_sessions`` row references its agent and model profile (RESTRICT) and,
optionally, its NXS-P06 customer / conversation and NXS-P11 call / NXS-P12 voice session
with COMPOSITE TENANT-AWARE foreign keys ``(organization_id, <id>) ->
(organization_id, id)`` (column-specific ``ON DELETE SET NULL``) so a session can only
ever bind to rows in its own Organization. ``ai_model_secrets`` holds Fernet ciphertext
only. ``ai_agent_turns`` stores the bounded conversation text (user input + final
answer), never reasoning. Provider account + external id is GLOBALLY unique.

Seeds the P13 permission-catalog delta: owner/admin receive every ``ai:*`` permission;
org_member receives ``ai:read`` and ``ai:use``.

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-09-10 16:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from nexus_ai.domain.auth.rbac import PERMISSION_IDS, ROLE_IDS, PermissionKey, RoleKey
from nexus_ai.infrastructure.rls import apply_tenant_rls, drop_tenant_rls

revision: str = "b7c8d9e0f1a2"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSONB = postgresql.JSONB(astext_type=sa.Text())
_NOW = sa.text("now()")

_P13_PERMISSIONS: tuple[tuple[PermissionKey, str], ...] = (
    (PermissionKey.AI_READ, "Read model provider accounts, model profiles, agents and sessions"),
    (PermissionKey.AI_USE, "Start / stop an agent session and submit a turn"),
    (
        PermissionKey.AI_CONFIGURE,
        "Create / update model provider accounts, model profiles and agents",
    ),
)
_MEMBER_PERMISSIONS = (PermissionKey.AI_READ, PermissionKey.AI_USE)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
    ]


def _org_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id"],
        ["organizations.id"],
        name=op.f(f"fk_{table}_organization_id_organizations"),
        ondelete="RESTRICT",
    )


def upgrade() -> None:
    op.create_table(
        "ai_model_provider_accounts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.String(length=24), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("api_base", sa.String(length=256), nullable=False),
        sa.Column("external_account_id", sa.String(length=128), nullable=False),
        sa.Column("credential_ref", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("configuration", _JSONB, nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "provider IN ('openai_compatible','fake')",
            name="ck_ai_model_provider_accounts_provider_known",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','DISABLED')",
            name="ck_ai_model_provider_accounts_status_known",
        ),
        _org_fk("ai_model_provider_accounts"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_model_provider_accounts")),
        sa.UniqueConstraint("organization_id", "id", name="uq_ai_model_provider_accounts_org_id"),
        sa.UniqueConstraint(
            "organization_id", "slug", name="uq_ai_model_provider_accounts_org_slug"
        ),
        sa.UniqueConstraint(
            "provider", "external_account_id", name="uq_ai_model_provider_accounts_provider"
        ),
    )
    op.create_index(
        "ix_ai_model_provider_accounts_organization_id",
        "ai_model_provider_accounts",
        ["organization_id"],
    )
    apply_tenant_rls(op, "ai_model_provider_accounts", allow_delete=True)

    op.create_table(
        "ai_model_secrets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("credential_ref", sa.String(length=128), nullable=False),
        sa.Column("credential_type", sa.String(length=24), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        *_timestamps(),
        _org_fk("ai_model_secrets"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_model_secrets")),
        sa.UniqueConstraint("organization_id", "credential_ref", name="uq_ai_model_secrets_ref"),
    )
    op.create_index("ix_ai_model_secrets_organization_id", "ai_model_secrets", ["organization_id"])
    apply_tenant_rls(op, "ai_model_secrets", allow_delete=True)

    op.create_table(
        "ai_model_profiles",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("model", sa.String(length=96), nullable=False),
        sa.Column("temperature", sa.Float(), nullable=True),
        sa.Column("max_output_tokens", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('ACTIVE','DISABLED')", name="ck_ai_model_profiles_status_known"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "account_id"],
            ["ai_model_provider_accounts.organization_id", "ai_model_provider_accounts.id"],
            name="fk_ai_model_profiles_org_account",
            ondelete="RESTRICT",
        ),
        _org_fk("ai_model_profiles"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_model_profiles")),
        sa.UniqueConstraint("organization_id", "id", name="uq_ai_model_profiles_org_id"),
        sa.UniqueConstraint("organization_id", "slug", name="uq_ai_model_profiles_org_slug"),
    )
    op.create_index(
        "ix_ai_model_profiles_organization_id", "ai_model_profiles", ["organization_id"]
    )
    op.create_index(
        "ix_ai_model_profiles_account", "ai_model_profiles", ["organization_id", "account_id"]
    )
    apply_tenant_rls(op, "ai_model_profiles", allow_delete=True)

    op.create_table(
        "ai_agents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("model_profile_id", sa.UUID(), nullable=False),
        sa.Column("system_instructions", sa.Text(), nullable=False),
        sa.Column("tool_keys", _JSONB, nullable=False),
        sa.Column("max_tool_iterations", sa.Integer(), nullable=False),
        sa.Column("max_output_tokens", sa.Integer(), nullable=True),
        sa.Column("temperature", sa.Float(), nullable=True),
        sa.Column("timeout_seconds", sa.Float(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("status IN ('ACTIVE','DISABLED')", name="ck_ai_agents_status_known"),
        sa.CheckConstraint(
            "max_tool_iterations BETWEEN 0 AND 32", name="ck_ai_agents_tool_iterations_bounds"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "model_profile_id"],
            ["ai_model_profiles.organization_id", "ai_model_profiles.id"],
            name="fk_ai_agents_org_profile",
            ondelete="RESTRICT",
        ),
        _org_fk("ai_agents"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_agents")),
        sa.UniqueConstraint("organization_id", "id", name="uq_ai_agents_org_id"),
        sa.UniqueConstraint("organization_id", "slug", name="uq_ai_agents_org_slug"),
    )
    op.create_index("ix_ai_agents_organization_id", "ai_agents", ["organization_id"])
    apply_tenant_rls(op, "ai_agents", allow_delete=True)

    op.create_table(
        "ai_agent_sessions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("model_profile_id", sa.UUID(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("state_rank", sa.Integer(), nullable=False),
        sa.Column("disposition", sa.String(length=16), nullable=True),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("initiator_user_id", sa.UUID(), nullable=False),
        sa.Column("initiator_session_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("customer_id", sa.UUID(), nullable=True),
        sa.Column("call_id", sa.UUID(), nullable=True),
        sa.Column("voice_session_id", sa.UUID(), nullable=True),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("turn_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("tool_call_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "state IN "
            "('PENDING','ACTIVE','WAITING_TOOL','RESPONDING','COMPLETED','FAILED','CANCELLED')",
            name="ck_ai_agent_sessions_state_known",
        ),
        sa.CheckConstraint(
            "channel IN ('API','VOICE','WHATSAPP','EMAIL','SMS')",
            name="ck_ai_agent_sessions_channel_known",
        ),
        sa.CheckConstraint(
            "state_rank BETWEEN 0 AND 4", name="ck_ai_agent_sessions_state_rank_bounds"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "agent_id"],
            ["ai_agents.organization_id", "ai_agents.id"],
            name="fk_ai_agent_sessions_org_agent",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "model_profile_id"],
            ["ai_model_profiles.organization_id", "ai_model_profiles.id"],
            name="fk_ai_agent_sessions_org_profile",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_ai_agent_sessions_org_customer",
            ondelete="SET NULL (customer_id)",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["conversations.organization_id", "conversations.id"],
            name="fk_ai_agent_sessions_org_conversation",
            ondelete="SET NULL (conversation_id)",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "call_id"],
            ["telephony_calls.organization_id", "telephony_calls.id"],
            name="fk_ai_agent_sessions_org_call",
            ondelete="SET NULL (call_id)",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "voice_session_id"],
            ["voice_sessions.organization_id", "voice_sessions.id"],
            name="fk_ai_agent_sessions_org_voice_session",
            ondelete="SET NULL (voice_session_id)",
        ),
        _org_fk("ai_agent_sessions"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_agent_sessions")),
        sa.UniqueConstraint("organization_id", "id", name="uq_ai_agent_sessions_org_id"),
        sa.UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_ai_agent_sessions_org_idempotency_key",
        ),
    )
    op.create_index(
        "ix_ai_agent_sessions_organization_id", "ai_agent_sessions", ["organization_id"]
    )
    op.create_index(
        "ix_ai_agent_sessions_agent", "ai_agent_sessions", ["organization_id", "agent_id"]
    )
    apply_tenant_rls(op, "ai_agent_sessions", allow_delete=True)

    op.create_table(
        "ai_agent_turns",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("input_text", sa.Text(), nullable=False),
        sa.Column("response_text", sa.Text(), nullable=True),
        sa.Column("input_char_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("response_char_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("model", sa.String(length=96), nullable=True),
        sa.Column("finish_reason", sa.String(length=24), nullable=True),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("tool_iterations", sa.Integer(), server_default="0", nullable=False),
        sa.Column("latency_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "state IN "
            "('PENDING','RUNNING','AWAITING_TOOLS','FINALIZING','COMPLETED','FAILED','CANCELLED')",
            name="ck_ai_agent_turns_state_known",
        ),
        sa.CheckConstraint(
            "channel IN ('API','VOICE','WHATSAPP','EMAIL','SMS')",
            name="ck_ai_agent_turns_channel_known",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "session_id"],
            ["ai_agent_sessions.organization_id", "ai_agent_sessions.id"],
            name="fk_ai_agent_turns_org_session",
            ondelete="CASCADE",
        ),
        _org_fk("ai_agent_turns"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_agent_turns")),
        sa.UniqueConstraint("organization_id", "id", name="uq_ai_agent_turns_org_id"),
        sa.UniqueConstraint(
            "organization_id", "session_id", "sequence", name="uq_ai_agent_turns_seq"
        ),
    )
    op.create_index("ix_ai_agent_turns_organization_id", "ai_agent_turns", ["organization_id"])
    op.create_index(
        "ix_ai_agent_turns_session", "ai_agent_turns", ["organization_id", "session_id"]
    )
    op.create_index(
        "uq_ai_agent_turns_idempotency",
        "ai_agent_turns",
        ["organization_id", "session_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    apply_tenant_rls(op, "ai_agent_turns", allow_delete=True)

    op.create_table(
        "ai_agent_tool_calls",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("turn_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("iteration", sa.Integer(), nullable=False),
        sa.Column("tool_key", sa.String(length=96), nullable=False),
        sa.Column("arguments_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("tool_result_class", sa.String(length=24), nullable=True),
        sa.Column("tool_status_code", sa.Integer(), nullable=True),
        sa.Column("denied_reason", sa.String(length=64), nullable=True),
        sa.Column("latency_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.CheckConstraint(
            "status IN ('REQUESTED','ALLOWED','DENIED','COMPLETED','FAILED')",
            name="ck_ai_agent_tool_calls_status_known",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "turn_id"],
            ["ai_agent_turns.organization_id", "ai_agent_turns.id"],
            name="fk_ai_agent_tool_calls_org_turn",
            ondelete="CASCADE",
        ),
        _org_fk("ai_agent_tool_calls"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_agent_tool_calls")),
        sa.UniqueConstraint("organization_id", "id", name="uq_ai_agent_tool_calls_org_id"),
    )
    op.create_index(
        "ix_ai_agent_tool_calls_organization_id", "ai_agent_tool_calls", ["organization_id"]
    )
    op.create_index(
        "ix_ai_agent_tool_calls_turn", "ai_agent_tool_calls", ["organization_id", "turn_id"]
    )
    apply_tenant_rls(op, "ai_agent_tool_calls")

    op.create_table(
        "ai_model_usage",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("model", sa.String(length=96), nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("tool_call_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("turn_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("latency_ms_total", sa.Integer(), server_default="0", nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "session_id"],
            ["ai_agent_sessions.organization_id", "ai_agent_sessions.id"],
            name="fk_ai_model_usage_org_session",
            ondelete="CASCADE",
        ),
        _org_fk("ai_model_usage"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_model_usage")),
        sa.UniqueConstraint("organization_id", "id", name="uq_ai_model_usage_org_id"),
        sa.UniqueConstraint("organization_id", "session_id", name="uq_ai_model_usage_session"),
    )
    op.create_index("ix_ai_model_usage_organization_id", "ai_model_usage", ["organization_id"])
    apply_tenant_rls(op, "ai_model_usage")

    _seed_permissions()


def _seed_permissions() -> None:
    permissions_table = sa.table(
        "permissions",
        sa.column("id", sa.UUID()),
        sa.column("permission_key", sa.String()),
        sa.column("description", sa.String()),
    )
    op.bulk_insert(
        permissions_table,
        [
            {"id": PERMISSION_IDS[key], "permission_key": key.value, "description": description}
            for key, description in _P13_PERMISSIONS
        ],
    )
    role_permissions_table = sa.table(
        "role_permissions",
        sa.column("role_id", sa.UUID()),
        sa.column("permission_id", sa.UUID()),
    )
    rows = [
        {"role_id": ROLE_IDS[role], "permission_id": PERMISSION_IDS[key]}
        for role in (RoleKey.ORG_OWNER, RoleKey.ORG_ADMIN)
        for key, _ in _P13_PERMISSIONS
    ]
    rows += [
        {"role_id": ROLE_IDS[RoleKey.ORG_MEMBER], "permission_id": PERMISSION_IDS[key]}
        for key in _MEMBER_PERMISSIONS
    ]
    op.bulk_insert(role_permissions_table, rows)


def downgrade() -> None:
    permission_ids = [str(PERMISSION_IDS[key]) for key, _ in _P13_PERMISSIONS]
    op.execute(
        sa.text("DELETE FROM role_permissions WHERE permission_id = ANY(:ids)").bindparams(
            sa.bindparam("ids", value=permission_ids)
        )
    )
    op.execute("DELETE FROM permissions WHERE permission_key LIKE 'ai:%'")

    for table in (
        "ai_model_usage",
        "ai_agent_tool_calls",
        "ai_agent_turns",
        "ai_agent_sessions",
        "ai_agents",
        "ai_model_profiles",
        "ai_model_secrets",
        "ai_model_provider_accounts",
    ):
        drop_tenant_rls(op, table)
        op.drop_table(table)
