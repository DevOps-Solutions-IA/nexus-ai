"""telephony foundation (NXS-P11: NXS-TEL-001)

Every table is TENANT-OWNED with forced RLS. A ``telephony_calls`` row references its
provider account and its owned caller-ID number with COMPOSITE TENANT-AWARE foreign keys
``(organization_id, <id>) -> (organization_id, id)`` so a call can only ever attach to
rows in its own Organization. A provider account (provider + external id) and a webhook
routing token are GLOBALLY unique; a phone number in E.164 form is GLOBALLY unique (it
belongs to exactly one Organization — caller-ID governance + inbound tenancy). The
per-provider-event idempotency log is unique per ``(organization_id, account_id,
provider_event_id)``. No SIP credential, ARI credential or provider token lives in a
call, an account row or an event — ``telephony_secrets`` holds Fernet ciphertext only.

Seeds the P11 permission-catalog delta: owner/admin receive every ``telephony:*``
permission; org_member receives ``telephony:read`` and ``telephony:call``.

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-09-10 01:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from nexus_ai.domain.auth.rbac import PERMISSION_IDS, ROLE_IDS, PermissionKey, RoleKey
from nexus_ai.infrastructure.rls import apply_tenant_rls, drop_tenant_rls

revision: str = "e3f4a5b6c7d8"
down_revision: str | None = "d2e3f4a5b6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSONB = postgresql.JSONB(astext_type=sa.Text())
_NOW = sa.text("now()")

_P11_PERMISSIONS: tuple[tuple[PermissionKey, str], ...] = (
    (PermissionKey.TELEPHONY_READ, "Read telephony accounts, numbers and calls"),
    (PermissionKey.TELEPHONY_CALL, "Place an outbound call and send DTMF"),
    (PermissionKey.TELEPHONY_HANGUP, "Hang up a call"),
    (PermissionKey.TELEPHONY_CONFIGURE, "Create / update telephony accounts and numbers"),
)
_MEMBER_PERMISSIONS = (PermissionKey.TELEPHONY_READ, PermissionKey.TELEPHONY_CALL)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "telephony_accounts",
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
            "provider IN ('asterisk','fake')", name="ck_telephony_accounts_provider_known"
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','DISABLED')", name="ck_telephony_accounts_status_known"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_telephony_accounts_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telephony_accounts")),
        sa.UniqueConstraint("organization_id", "id", name="uq_telephony_accounts_org_id"),
        sa.UniqueConstraint("organization_id", "slug", name="uq_telephony_accounts_org_slug"),
        sa.UniqueConstraint(
            "provider", "external_account_id", name="uq_telephony_accounts_provider"
        ),
        sa.UniqueConstraint("webhook_token", name="uq_telephony_accounts_webhook_token"),
    )
    op.create_index(
        "ix_telephony_accounts_organization_id",
        "telephony_accounts",
        ["organization_id"],
        unique=False,
    )
    apply_tenant_rls(op, "telephony_accounts", allow_delete=True)

    op.create_table(
        "telephony_secrets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("credential_ref", sa.String(length=128), nullable=False),
        sa.Column("credential_type", sa.String(length=24), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_telephony_secrets_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telephony_secrets")),
        sa.UniqueConstraint("organization_id", "credential_ref", name="uq_telephony_secrets_ref"),
    )
    op.create_index(
        "ix_telephony_secrets_organization_id",
        "telephony_secrets",
        ["organization_id"],
        unique=False,
    )
    apply_tenant_rls(op, "telephony_secrets", allow_delete=True)

    op.create_table(
        "telephony_phone_numbers",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("e164", sa.String(length=16), nullable=False),
        sa.Column("display_name", sa.String(length=80), nullable=True),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("inbound_enabled", sa.Boolean(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            r"e164 ~ '^\+[1-9][0-9]{6,14}$'", name="ck_telephony_phone_numbers_e164_form"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "account_id"],
            ["telephony_accounts.organization_id", "telephony_accounts.id"],
            name="fk_telephony_phone_numbers_org_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_telephony_phone_numbers_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telephony_phone_numbers")),
        sa.UniqueConstraint("organization_id", "id", name="uq_telephony_phone_numbers_org_id"),
        sa.UniqueConstraint("e164", name="uq_telephony_phone_numbers_e164"),
    )
    op.create_index(
        "ix_telephony_phone_numbers_organization_id",
        "telephony_phone_numbers",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_telephony_phone_numbers_account",
        "telephony_phone_numbers",
        ["organization_id", "account_id"],
        unique=False,
    )
    apply_tenant_rls(op, "telephony_phone_numbers", allow_delete=True)

    op.create_table(
        "telephony_calls",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("from_number_id", sa.UUID(), nullable=True),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("disposition", sa.String(length=16), nullable=True),
        sa.Column("state_rank", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=24), nullable=False),
        sa.Column("provider_call_id", sa.String(length=200), nullable=True),
        sa.Column("from_address", sa.String(length=128), nullable=False),
        sa.Column("to_address", sa.String(length=128), nullable=False),
        sa.Column("legs", _JSONB, nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("provider_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_sequence", sa.Integer(), nullable=True),
        sa.Column("ringing_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "direction IN ('INBOUND','OUTBOUND')", name="ck_telephony_calls_direction_known"
        ),
        sa.CheckConstraint(
            "state IN ('CREATED','RINGING','EARLY_MEDIA','ANSWERED','BRIDGED','ENDING',"
            "'COMPLETED','FAILED','CANCELLED','BUSY','NO_ANSWER')",
            name="ck_telephony_calls_state_known",
        ),
        sa.CheckConstraint(
            "state_rank BETWEEN 0 AND 6", name="ck_telephony_calls_state_rank_bounds"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "account_id"],
            ["telephony_accounts.organization_id", "telephony_accounts.id"],
            name="fk_telephony_calls_org_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "from_number_id"],
            ["telephony_phone_numbers.organization_id", "telephony_phone_numbers.id"],
            name="fk_telephony_calls_org_from_number",
            ondelete="SET NULL (from_number_id)",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_telephony_calls_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telephony_calls")),
        sa.UniqueConstraint("organization_id", "id", name="uq_telephony_calls_org_id"),
        sa.UniqueConstraint(
            "organization_id",
            "account_id",
            "provider_call_id",
            name="uq_telephony_calls_provider_call_id",
        ),
        sa.UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_telephony_calls_org_idempotency_key"
        ),
    )
    op.create_index(
        "ix_telephony_calls_organization_id",
        "telephony_calls",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_telephony_calls_state", "telephony_calls", ["organization_id", "state"], unique=False
    )
    op.create_index(
        "ix_telephony_calls_account_created",
        "telephony_calls",
        ["organization_id", "account_id", "created_at"],
        unique=False,
    )
    apply_tenant_rls(op, "telephony_calls", allow_delete=True)

    op.create_table(
        "telephony_call_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("call_id", sa.UUID(), nullable=True),
        sa.Column("provider_event_id", sa.String(length=200), nullable=False),
        sa.Column("provider_call_id", sa.String(length=200), nullable=False),
        sa.Column("event_type", sa.String(length=16), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("applied_state", sa.String(length=16), nullable=True),
        sa.Column("detail", _JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.CheckConstraint(
            "event_type IN ('STATE','DTMF','MEDIA')",
            name="ck_telephony_call_events_type_known",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "account_id"],
            ["telephony_accounts.organization_id", "telephony_accounts.id"],
            name="fk_telephony_call_events_org_account",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_telephony_call_events_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telephony_call_events")),
        sa.UniqueConstraint(
            "organization_id",
            "account_id",
            "provider_event_id",
            name="uq_telephony_call_events_provider_event",
        ),
    )
    op.create_index(
        "ix_telephony_call_events_organization_id",
        "telephony_call_events",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_telephony_call_events_call",
        "telephony_call_events",
        ["organization_id", "call_id", "created_at"],
        unique=False,
    )
    apply_tenant_rls(op, "telephony_call_events")

    op.create_table(
        "telephony_media_sessions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("call_id", sa.UUID(), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("bridge_id", sa.String(length=200), nullable=True),
        sa.Column("stream_id", sa.String(length=200), nullable=True),
        sa.Column("codec", _JSONB, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "state IN ('PENDING','ACTIVE','STOPPED')",
            name="ck_telephony_media_sessions_state_known",
        ),
        sa.CheckConstraint(
            "direction IN ('INBOUND','OUTBOUND','BIDIRECTIONAL')",
            name="ck_telephony_media_sessions_direction_known",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "call_id"],
            ["telephony_calls.organization_id", "telephony_calls.id"],
            name="fk_telephony_media_sessions_org_call",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_telephony_media_sessions_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telephony_media_sessions")),
        sa.UniqueConstraint("organization_id", "id", name="uq_telephony_media_sessions_org_id"),
    )
    op.create_index(
        "ix_telephony_media_sessions_organization_id",
        "telephony_media_sessions",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_telephony_media_sessions_call",
        "telephony_media_sessions",
        ["organization_id", "call_id"],
        unique=False,
    )
    apply_tenant_rls(op, "telephony_media_sessions")

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
            for key, description in _P11_PERMISSIONS
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
        for key, _ in _P11_PERMISSIONS
    ] + [
        {"role_id": ROLE_IDS[RoleKey.ORG_MEMBER], "permission_id": PERMISSION_IDS[key]}
        for key in _MEMBER_PERMISSIONS
    ]
    op.bulk_insert(role_permissions_table, rows)


def downgrade() -> None:
    permission_ids = [str(PERMISSION_IDS[key]) for key, _ in _P11_PERMISSIONS]
    op.execute(
        sa.text("DELETE FROM role_permissions WHERE permission_id = ANY(:ids)").bindparams(
            sa.bindparam("ids", value=permission_ids)
        )
    )
    op.execute("DELETE FROM permissions WHERE permission_key LIKE 'telephony:%'")

    for table in (
        "telephony_media_sessions",
        "telephony_call_events",
        "telephony_calls",
        "telephony_phone_numbers",
        "telephony_secrets",
        "telephony_accounts",
    ):
        drop_tenant_rls(op, table)
        op.drop_table(table)
