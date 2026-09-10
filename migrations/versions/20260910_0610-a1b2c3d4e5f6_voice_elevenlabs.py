"""voice elevenlabs (NXS-P12: NXS-VOICE-001 / NXS-EL-001)

Every table is TENANT-OWNED with forced RLS. A ``voice_sessions`` row references its
NXS-P11 call and media session with COMPOSITE TENANT-AWARE foreign keys
``(organization_id, <id>) -> (organization_id, id)`` (RESTRICT) so a voice session can
only ever attach to rows in its own Organization — NXS-P11 stays authoritative for the
call. A provider account (provider + external id) and a webhook routing token are
GLOBALLY unique. At most one LIVE voice session per media session (partial unique
index). The per-provider-event idempotency log is unique per
``(organization_id, account_id, provider_event_id)``. No provider API key, signed
WebSocket URL, raw provider frame or audio lives in a table — ``voice_secrets`` holds
Fernet ciphertext only.

Seeds the P12 permission-catalog delta: owner/admin receive every ``voice:*``
permission; org_member receives ``voice:read`` and ``voice:use``.

Revision ID: a1b2c3d4e5f6
Revises: f4a5b6c7d8e9
Create Date: 2026-09-10 06:10:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from nexus_ai.domain.auth.rbac import PERMISSION_IDS, ROLE_IDS, PermissionKey, RoleKey
from nexus_ai.infrastructure.rls import apply_tenant_rls, drop_tenant_rls

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "f4a5b6c7d8e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSONB = postgresql.JSONB(astext_type=sa.Text())
_NOW = sa.text("now()")

_P12_PERMISSIONS: tuple[tuple[PermissionKey, str], ...] = (
    (PermissionKey.VOICE_READ, "Read voice provider accounts, profiles and sessions"),
    (PermissionKey.VOICE_USE, "Start / stop a voice session and request a handoff"),
    (PermissionKey.VOICE_CONFIGURE, "Create / update voice provider accounts and profiles"),
)
_MEMBER_PERMISSIONS = (PermissionKey.VOICE_READ, PermissionKey.VOICE_USE)

_LIVE_STATES = "'PENDING','CONNECTING','CONNECTED','STREAMING','ENDING'"


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "voice_provider_accounts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.String(length=24), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("external_account_id", sa.String(length=128), nullable=False),
        sa.Column("credential_ref", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("configuration", _JSONB, nullable=False),
        sa.Column("webhook_token", sa.String(length=96), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "provider IN ('elevenlabs','fake')", name="ck_voice_provider_accounts_provider_known"
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','DISABLED')", name="ck_voice_provider_accounts_status_known"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_voice_provider_accounts_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_voice_provider_accounts")),
        sa.UniqueConstraint("organization_id", "id", name="uq_voice_provider_accounts_org_id"),
        sa.UniqueConstraint("organization_id", "slug", name="uq_voice_provider_accounts_org_slug"),
        sa.UniqueConstraint(
            "provider", "external_account_id", name="uq_voice_provider_accounts_provider"
        ),
        sa.UniqueConstraint("webhook_token", name="uq_voice_provider_accounts_webhook_token"),
    )
    op.create_index(
        "ix_voice_provider_accounts_organization_id",
        "voice_provider_accounts",
        ["organization_id"],
    )
    apply_tenant_rls(op, "voice_provider_accounts", allow_delete=True)

    op.create_table(
        "voice_secrets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("credential_ref", sa.String(length=128), nullable=False),
        sa.Column("credential_type", sa.String(length=24), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_voice_secrets_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_voice_secrets")),
        sa.UniqueConstraint("organization_id", "credential_ref", name="uq_voice_secrets_ref"),
    )
    op.create_index("ix_voice_secrets_organization_id", "voice_secrets", ["organization_id"])
    apply_tenant_rls(op, "voice_secrets", allow_delete=True)

    op.create_table(
        "voice_profiles",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("provider_voice_ref", sa.String(length=128), nullable=False),
        sa.Column("provider_model_ref", sa.String(length=96), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("input_format", _JSONB, nullable=False),
        sa.Column("output_format", _JSONB, nullable=False),
        sa.Column("config", _JSONB, nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('ACTIVE','DISABLED')", name="ck_voice_profiles_status_known"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "account_id"],
            ["voice_provider_accounts.organization_id", "voice_provider_accounts.id"],
            name="fk_voice_profiles_org_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_voice_profiles_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_voice_profiles")),
        sa.UniqueConstraint("organization_id", "id", name="uq_voice_profiles_org_id"),
        sa.UniqueConstraint("organization_id", "slug", name="uq_voice_profiles_org_slug"),
    )
    op.create_index("ix_voice_profiles_organization_id", "voice_profiles", ["organization_id"])
    op.create_index(
        "ix_voice_profiles_account", "voice_profiles", ["organization_id", "account_id"]
    )
    apply_tenant_rls(op, "voice_profiles", allow_delete=True)

    op.create_table(
        "voice_sessions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("voice_profile_id", sa.UUID(), nullable=True),
        sa.Column("call_id", sa.UUID(), nullable=False),
        sa.Column("media_session_id", sa.UUID(), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("disposition", sa.String(length=16), nullable=True),
        sa.Column("state_rank", sa.Integer(), nullable=False),
        sa.Column("handoff_state", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=24), nullable=False),
        sa.Column("provider_session_id", sa.String(length=200), nullable=True),
        sa.Column("bridge_ref", sa.String(length=200), nullable=True),
        sa.Column("negotiated_format", _JSONB, nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("latency", _JSONB, nullable=True),
        sa.Column("usage", _JSONB, nullable=True),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column("provider_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_sequence", sa.Integer(), nullable=True),
        sa.Column("connecting_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "direction IN ('INBOUND','OUTBOUND')", name="ck_voice_sessions_direction_known"
        ),
        sa.CheckConstraint(
            "state IN ('PENDING','CONNECTING','CONNECTED','STREAMING','ENDING',"
            "'COMPLETED','FAILED','CANCELLED')",
            name="ck_voice_sessions_state_known",
        ),
        sa.CheckConstraint(
            "handoff_state IN ('AI','PENDING_HUMAN','HUMAN')",
            name="ck_voice_sessions_handoff_known",
        ),
        sa.CheckConstraint(
            "state_rank BETWEEN 0 AND 5", name="ck_voice_sessions_state_rank_bounds"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "account_id"],
            ["voice_provider_accounts.organization_id", "voice_provider_accounts.id"],
            name="fk_voice_sessions_org_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "voice_profile_id"],
            ["voice_profiles.organization_id", "voice_profiles.id"],
            name="fk_voice_sessions_org_profile",
            ondelete="SET NULL (voice_profile_id)",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "call_id"],
            ["telephony_calls.organization_id", "telephony_calls.id"],
            name="fk_voice_sessions_org_call",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "media_session_id"],
            ["telephony_media_sessions.organization_id", "telephony_media_sessions.id"],
            name="fk_voice_sessions_org_media_session",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_voice_sessions_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_voice_sessions")),
        sa.UniqueConstraint("organization_id", "id", name="uq_voice_sessions_org_id"),
        sa.UniqueConstraint(
            "organization_id",
            "account_id",
            "provider_session_id",
            name="uq_voice_sessions_provider_session_id",
        ),
        sa.UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_voice_sessions_org_idempotency_key"
        ),
    )
    op.create_index("ix_voice_sessions_organization_id", "voice_sessions", ["organization_id"])
    op.create_index("ix_voice_sessions_call", "voice_sessions", ["organization_id", "call_id"])
    op.create_index(
        "uq_voice_sessions_one_live_per_media",
        "voice_sessions",
        ["organization_id", "media_session_id"],
        unique=True,
        postgresql_where=sa.text(f"state IN ({_LIVE_STATES})"),
    )
    apply_tenant_rls(op, "voice_sessions", allow_delete=True)

    op.create_table(
        "voice_provider_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("provider_event_id", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("detail", _JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.CheckConstraint(
            "kind IN ('SESSION','TRANSCRIPT','MEDIA','USAGE','ERROR')",
            name="ck_voice_provider_events_kind_known",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "account_id"],
            ["voice_provider_accounts.organization_id", "voice_provider_accounts.id"],
            name="fk_voice_provider_events_org_account",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_voice_provider_events_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_voice_provider_events")),
        sa.UniqueConstraint(
            "organization_id",
            "account_id",
            "provider_event_id",
            name="uq_voice_provider_events_provider_event",
        ),
    )
    op.create_index(
        "ix_voice_provider_events_organization_id",
        "voice_provider_events",
        ["organization_id"],
    )
    op.create_index(
        "ix_voice_provider_events_session",
        "voice_provider_events",
        ["organization_id", "session_id", "created_at"],
    )
    apply_tenant_rls(op, "voice_provider_events")

    op.create_table(
        "voice_usage_records",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("audio_seconds_in", sa.Float(), nullable=False),
        sa.Column("audio_seconds_out", sa.Float(), nullable=False),
        sa.Column("provider_characters", sa.Integer(), nullable=False),
        sa.Column("interruptions", sa.Integer(), nullable=False),
        sa.Column("time_to_first_audio_ms", sa.Integer(), nullable=True),
        sa.Column("session_duration_ms", sa.Integer(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "session_id"],
            ["voice_sessions.organization_id", "voice_sessions.id"],
            name="fk_voice_usage_records_org_session",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_voice_usage_records_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_voice_usage_records")),
        sa.UniqueConstraint("organization_id", "id", name="uq_voice_usage_records_org_id"),
        sa.UniqueConstraint("organization_id", "session_id", name="uq_voice_usage_records_session"),
    )
    op.create_index(
        "ix_voice_usage_records_organization_id",
        "voice_usage_records",
        ["organization_id"],
    )
    apply_tenant_rls(op, "voice_usage_records")

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
            for key, description in _P12_PERMISSIONS
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
        for key, _ in _P12_PERMISSIONS
    ] + [
        {"role_id": ROLE_IDS[RoleKey.ORG_MEMBER], "permission_id": PERMISSION_IDS[key]}
        for key in _MEMBER_PERMISSIONS
    ]
    op.bulk_insert(role_permissions_table, rows)


def downgrade() -> None:
    permission_ids = [str(PERMISSION_IDS[key]) for key, _ in _P12_PERMISSIONS]
    op.execute(
        sa.text("DELETE FROM role_permissions WHERE permission_id = ANY(:ids)").bindparams(
            sa.bindparam("ids", value=permission_ids)
        )
    )
    op.execute("DELETE FROM permissions WHERE permission_key LIKE 'voice:%'")

    for table in (
        "voice_usage_records",
        "voice_provider_events",
        "voice_sessions",
        "voice_profiles",
        "voice_secrets",
        "voice_provider_accounts",
    ):
        drop_tenant_rls(op, table)
        op.drop_table(table)
