"""messaging channels (NXS-P09: NXS-WA-001, NXS-EMAIL-001, NXS-SMS-001)

Every table is TENANT-OWNED with forced RLS. A ``messaging_messages`` row references its
Conversation, Customer and messaging account with COMPOSITE TENANT-AWARE foreign keys
``(organization_id, <parent_id>) -> (organization_id, id)`` so a message can only attach
to rows in its own Organization (ADR-0052). A provider account
(channel + provider + external id) and a webhook routing token are GLOBALLY unique so no
two Organizations can adopt the same phone number / mailbox / token. No provider secret
is stored in a message or account row — ``messaging_secrets`` holds Fernet ciphertext
only (the same encryption seam as the NXS-P07 vault; a dedicated table isolates the two
credential planes).

The migration also seeds the P09 permission-catalog delta: owner/admin receive every
``messaging:*`` permission; org_member receives ``messaging:read`` and
``messaging:send`` (send through a configured account, but never manage one).

Revision ID: b92b586b3e23
Revises: 8c2d3e4f5a61
Create Date: 2026-09-08 19:06:41+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from nexus_ai.domain.auth.rbac import PERMISSION_IDS, ROLE_IDS, PermissionKey, RoleKey
from nexus_ai.infrastructure.rls import apply_tenant_rls, drop_tenant_rls

revision: str = "b92b586b3e23"
down_revision: str | None = "8c2d3e4f5a61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSONB = postgresql.JSONB(astext_type=sa.Text())
_NOW = sa.text("now()")

_P09_PERMISSIONS: tuple[tuple[PermissionKey, str], ...] = (
    (PermissionKey.MESSAGING_READ, "Read messaging accounts and messages"),
    (PermissionKey.MESSAGING_SEND, "Send a message through a configured channel account"),
    (PermissionKey.MESSAGING_MANAGE_ACCOUNTS, "Create / update / disable channel accounts"),
)
_MEMBER_PERMISSIONS = (PermissionKey.MESSAGING_READ, PermissionKey.MESSAGING_SEND)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "messaging_accounts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=48), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("external_account_id", sa.String(length=128), nullable=False),
        sa.Column("sender_identity", sa.String(length=320), nullable=False),
        sa.Column("credential_ref", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("configuration", _JSONB, nullable=False),
        sa.Column("webhook_token", sa.String(length=96), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "channel IN ('WHATSAPP','EMAIL','SMS')",
            name=op.f("ck_messaging_accounts_channel_known"),
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','DISABLED')", name=op.f("ck_messaging_accounts_status_known")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_messaging_accounts_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messaging_accounts")),
        sa.UniqueConstraint("organization_id", "id", name="uq_messaging_accounts_org_id"),
        sa.UniqueConstraint("organization_id", "slug", name="uq_messaging_accounts_org_slug"),
        sa.UniqueConstraint(
            "channel", "provider", "external_account_id", name="uq_messaging_accounts_provider"
        ),
        sa.UniqueConstraint("webhook_token", name="uq_messaging_accounts_webhook_token"),
    )
    op.create_index(
        op.f("ix_messaging_accounts_organization_id"),
        "messaging_accounts",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_messaging_accounts_channel",
        "messaging_accounts",
        ["organization_id", "channel"],
        unique=False,
    )
    apply_tenant_rls(op, "messaging_accounts", allow_delete=True)

    op.create_table(
        "messaging_secrets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("credential_ref", sa.String(length=128), nullable=False),
        sa.Column("credential_type", sa.String(length=24), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_messaging_secrets_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messaging_secrets")),
        sa.UniqueConstraint("organization_id", "credential_ref", name="uq_messaging_secrets_ref"),
    )
    op.create_index(
        op.f("ix_messaging_secrets_organization_id"),
        "messaging_secrets",
        ["organization_id"],
        unique=False,
    )
    apply_tenant_rls(op, "messaging_secrets", allow_delete=True)

    op.create_table(
        "messaging_inbound_receipts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("event_id", sa.String(length=400), nullable=False),
        sa.Column("message_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "account_id"],
            ["messaging_accounts.organization_id", "messaging_accounts.id"],
            name="fk_messaging_inbound_receipts_org_account",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_messaging_inbound_receipts_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messaging_inbound_receipts")),
        sa.UniqueConstraint(
            "organization_id",
            "account_id",
            "event_id",
            name="uq_messaging_inbound_receipts_event",
        ),
    )
    op.create_index(
        op.f("ix_messaging_inbound_receipts_organization_id"),
        "messaging_inbound_receipts",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_messaging_inbound_receipts_account",
        "messaging_inbound_receipts",
        ["organization_id", "account_id", "created_at"],
        unique=False,
    )
    apply_tenant_rls(op, "messaging_inbound_receipts")

    op.create_table(
        "messaging_send_idempotency",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("message_id", sa.UUID(), nullable=True),
        sa.Column("result_json", _JSONB, nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        *_timestamps(),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING','COMPLETED','FAILED')",
            name=op.f("ck_messaging_send_idempotency_status_known"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "account_id"],
            ["messaging_accounts.organization_id", "messaging_accounts.id"],
            name="fk_messaging_send_idempotency_org_account",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_messaging_send_idempotency_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messaging_send_idempotency")),
        sa.UniqueConstraint(
            "organization_id",
            "account_id",
            "idempotency_key",
            name="uq_messaging_send_idempotency_key",
        ),
    )
    op.create_index(
        op.f("ix_messaging_send_idempotency_organization_id"),
        "messaging_send_idempotency",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_messaging_send_idempotency_expiry",
        "messaging_send_idempotency",
        ["organization_id", "expires_at"],
        unique=False,
    )
    apply_tenant_rls(op, "messaging_send_idempotency", allow_delete=True)

    op.create_table(
        "messaging_messages",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=True),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=48), nullable=False),
        sa.Column("provider_message_id", sa.String(length=200), nullable=True),
        sa.Column("sender", _JSONB, nullable=False),
        sa.Column("recipients", _JSONB, nullable=False),
        sa.Column("content", _JSONB, nullable=False),
        sa.Column("email", _JSONB, nullable=True),
        sa.Column("sms", _JSONB, nullable=True),
        sa.Column("reply_to_message_id", sa.UUID(), nullable=True),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("provider_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "channel IN ('WHATSAPP','EMAIL','SMS')",
            name=op.f("ck_messaging_messages_channel_known"),
        ),
        sa.CheckConstraint(
            "direction IN ('INBOUND','OUTBOUND')",
            name=op.f("ck_messaging_messages_direction_known"),
        ),
        sa.CheckConstraint(
            "status IN ('RECEIVED','QUEUED','SENDING','SENT','DELIVERED','READ','FAILED')",
            name=op.f("ck_messaging_messages_status_known"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["conversations.organization_id", "conversations.id"],
            name="fk_messaging_messages_org_conversation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_messaging_messages_org_customer",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "account_id"],
            ["messaging_accounts.organization_id", "messaging_accounts.id"],
            name="fk_messaging_messages_org_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_messaging_messages_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messaging_messages")),
        sa.UniqueConstraint("organization_id", "id", name="uq_messaging_messages_org_id"),
        sa.UniqueConstraint(
            "organization_id",
            "account_id",
            "direction",
            "provider_message_id",
            name="uq_messaging_messages_provider_id",
        ),
    )
    op.create_index(
        op.f("ix_messaging_messages_organization_id"),
        "messaging_messages",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_messaging_messages_conversation",
        "messaging_messages",
        ["organization_id", "conversation_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_messaging_messages_status",
        "messaging_messages",
        ["organization_id", "status"],
        unique=False,
    )
    apply_tenant_rls(op, "messaging_messages")

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
            for key, description in _P09_PERMISSIONS
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
        for key, _ in _P09_PERMISSIONS
    ] + [
        {"role_id": ROLE_IDS[RoleKey.ORG_MEMBER], "permission_id": PERMISSION_IDS[key]}
        for key in _MEMBER_PERMISSIONS
    ]
    op.bulk_insert(role_permissions_table, rows)


def downgrade() -> None:
    permission_ids = [str(PERMISSION_IDS[key]) for key, _ in _P09_PERMISSIONS]
    op.execute(
        sa.text("DELETE FROM role_permissions WHERE permission_id = ANY(:ids)").bindparams(
            sa.bindparam("ids", value=permission_ids)
        )
    )
    op.execute("DELETE FROM permissions WHERE permission_key LIKE 'messaging:%'")

    for table in (
        "messaging_messages",
        "messaging_send_idempotency",
        "messaging_inbound_receipts",
        "messaging_secrets",
        "messaging_accounts",
    ):
        drop_tenant_rls(op, table)
        op.drop_table(table)
