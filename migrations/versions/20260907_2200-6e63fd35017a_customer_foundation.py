"""customer foundation (NXS-CUSTOMER-001)

The P06 customer identity and conversation schema. Every table is TENANT-OWNED with
forced RLS. Cross-tenant attachment safety is enforced at TWO levels (ADR-0052):

* forced RLS (runtime),
* COMPOSITE TENANT-AWARE foreign keys: ``(organization_id, <parent_id>)`` references
  ``(organization_id, id)`` on the parent table (schema level) — the database itself
  refuses a Customer in Organization A from referencing a Conversation in
  Organization B.

Identity invariants are unique constraints:

* ``uq_customer_identities_canonical`` on (organization_id, identity_type,
  normalized_value) — one canonical identity per Customer per Organization; it also
  serves as the identity-resolution index.
* ``uq_conversations_external_thread`` on (organization_id, channel,
  provider_namespace, external_thread_id) — nullable external fields make PostgreSQL
  treat NULLs as distinct, so the key is enforced exactly when present.
* ``uq_conversation_activities_dedup`` on (organization_id, dedup_key) — replay-safe
  timeline appends exactly when a dedup key is present.

The migration also seeds the P06 permission catalog delta: owner/admin receive all
eight P06 permissions; org_member receives customer:read, customer:create,
customer:identity:link, conversation:read, conversation:create, conversation:close
and customer:timeline:read (customer:update is owner/admin-only).

Revision ID: 6e63fd35017a
Revises: 04a4640c5b95
Create Date: 2026-09-07 22:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from nexus_ai.domain.auth.rbac import (
    PERMISSION_IDS,
    ROLE_IDS,
    PermissionKey,
    RoleKey,
)
from nexus_ai.infrastructure.rls import apply_tenant_rls, drop_tenant_rls

revision: str = "6e63fd35017a"
down_revision: str | None = "04a4640c5b95"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("preferred_locale", sa.String(length=32), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'SUSPENDED')", name=op.f("ck_customers_status_known")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_customers_version_positive")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_customers_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customers")),
        sa.UniqueConstraint("organization_id", "id", name="uq_customers_org_id"),
    )
    op.create_index("ix_customers_status", "customers", ["status"], unique=False)
    op.create_index("ix_customers_organization_id", "customers", ["organization_id"], unique=False)
    apply_tenant_rls(op, "customers")

    op.create_table(
        "customer_identities",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("identity_type", sa.String(length=16), nullable=False),
        sa.Column("normalized_value", sa.String(length=320), nullable=False),
        sa.Column("verification_state", sa.String(length=16), nullable=False),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "identity_type IN ('EMAIL', 'PHONE', 'EXTERNAL_ID')",
            name=op.f("ck_customer_identities_identity_type_known"),
        ),
        sa.CheckConstraint(
            "verification_state IN ('UNVERIFIED', 'VERIFIED', 'REVOKED')",
            name=op.f("ck_customer_identities_verification_state_known"),
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'REVOKED')", name=op.f("ck_customer_identities_status_known")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_customer_identities_org_customer_customers",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_customer_identities_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customer_identities")),
        sa.UniqueConstraint(
            "organization_id",
            "identity_type",
            "normalized_value",
            name="uq_customer_identities_canonical",
        ),
    )
    op.create_index(
        "ix_customer_identities_customer",
        "customer_identities",
        ["organization_id", "customer_id"],
        unique=False,
    )
    op.create_index(
        "ix_customer_identities_organization_id",
        "customer_identities",
        ["organization_id"],
        unique=False,
    )
    apply_tenant_rls(op, "customer_identities")

    op.create_table(
        "conversations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=True),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("provider_namespace", sa.String(length=48), nullable=True),
        sa.Column("external_thread_id", sa.String(length=128), nullable=True),
        sa.Column("subject", sa.String(length=200), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "opened_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("channel != ''", name=op.f("ck_conversations_channel_nonempty")),
        sa.CheckConstraint(
            "status IN ('PENDING', 'OPEN', 'CLOSED')", name=op.f("ck_conversations_status_known")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_conversations_version_positive")),
        sa.ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_conversations_org_customer_customers",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_conversations_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
        sa.UniqueConstraint("organization_id", "id", name="uq_conversations_org_id"),
        sa.UniqueConstraint(
            "organization_id",
            "channel",
            "provider_namespace",
            "external_thread_id",
            name="uq_conversations_external_thread",
        ),
    )
    op.create_index(
        "ix_conversations_customer",
        "conversations",
        ["organization_id", "customer_id"],
        unique=False,
    )
    op.create_index("ix_conversations_status", "conversations", ["status"], unique=False)
    op.create_index(
        "ix_conversations_organization_id", "conversations", ["organization_id"], unique=False
    )
    apply_tenant_rls(op, "conversations")

    op.create_table(
        "conversation_participants",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("participant_type", sa.String(length=16), nullable=False),
        sa.Column("participant_ref", sa.String(length=160), nullable=False),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "participant_type IN ('CUSTOMER', 'HUMAN_AGENT', 'AI_AGENT', 'SYSTEM')",
            name=op.f("ck_conversation_participants_participant_type_known"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["conversations.organization_id", "conversations.id"],
            name="fk_conversation_participants_org_conversation_conversations",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_conversation_participants_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversation_participants")),
        sa.UniqueConstraint(
            "organization_id",
            "conversation_id",
            "participant_type",
            "participant_ref",
            name="uq_conversation_participants_identity",
        ),
    )
    op.create_index(
        "ix_conversation_participants_conversation",
        "conversation_participants",
        ["organization_id", "conversation_id"],
        unique=False,
    )
    op.create_index(
        "ix_conversation_participants_organization_id",
        "conversation_participants",
        ["organization_id"],
        unique=False,
    )
    apply_tenant_rls(op, "conversation_participants")

    op.create_table(
        "conversation_activities",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=True),
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("activity_type", sa.String(length=48), nullable=False),
        sa.Column("dedup_key", sa.String(length=160), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_conversation_activities_org_customer_customers",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["conversations.organization_id", "conversations.id"],
            name="fk_conversation_activities_org_conversation_conversations",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_conversation_activities_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversation_activities")),
        sa.UniqueConstraint(
            "organization_id", "dedup_key", name="uq_conversation_activities_dedup"
        ),
    )
    op.create_index(
        "ix_conversation_activities_timeline",
        "conversation_activities",
        ["organization_id", "customer_id", "occurred_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_conversation_activities_organization_id",
        "conversation_activities",
        ["organization_id"],
        unique=False,
    )
    apply_tenant_rls(op, "conversation_activities")

    # --- P06 permission catalog seed (deterministic ids) ------------------------------
    permissions_table = sa.table(
        "permissions",
        sa.column("id", sa.UUID()),
        sa.column("permission_key", sa.String()),
        sa.column("description", sa.String()),
    )
    op.bulk_insert(
        permissions_table,
        [
            {
                "id": PERMISSION_IDS[PermissionKey.CUSTOMER_READ],
                "permission_key": "customer:read",
                "description": "Read Customers and their identities in this Organization",
            },
            {
                "id": PERMISSION_IDS[PermissionKey.CUSTOMER_CREATE],
                "permission_key": "customer:create",
                "description": "Create (resolve-or-create) Customers",
            },
            {
                "id": PERMISSION_IDS[PermissionKey.CUSTOMER_UPDATE],
                "permission_key": "customer:update",
                "description": "Update Customer profiles",
            },
            {
                "id": PERMISSION_IDS[PermissionKey.CUSTOMER_IDENTITY_LINK],
                "permission_key": "customer:identity:link",
                "description": "Link and manage Customer channel identities",
            },
            {
                "id": PERMISSION_IDS[PermissionKey.CONVERSATION_READ],
                "permission_key": "conversation:read",
                "description": "Read Conversations",
            },
            {
                "id": PERMISSION_IDS[PermissionKey.CONVERSATION_CREATE],
                "permission_key": "conversation:create",
                "description": "Open (resolve-or-create) Conversations",
            },
            {
                "id": PERMISSION_IDS[PermissionKey.CONVERSATION_CLOSE],
                "permission_key": "conversation:close",
                "description": "Close and reopen Conversations",
            },
            {
                "id": PERMISSION_IDS[PermissionKey.CUSTOMER_TIMELINE_READ],
                "permission_key": "customer:timeline:read",
                "description": "Read the unified Customer timeline",
            },
        ],
    )
    role_permissions_table = sa.table(
        "role_permissions",
        sa.column("role_id", sa.UUID()),
        sa.column("permission_id", sa.UUID()),
    )
    owner_admin_permissions = (
        PermissionKey.CUSTOMER_READ,
        PermissionKey.CUSTOMER_CREATE,
        PermissionKey.CUSTOMER_UPDATE,
        PermissionKey.CUSTOMER_IDENTITY_LINK,
        PermissionKey.CONVERSATION_READ,
        PermissionKey.CONVERSATION_CREATE,
        PermissionKey.CONVERSATION_CLOSE,
        PermissionKey.CUSTOMER_TIMELINE_READ,
    )
    member_permissions = tuple(
        p for p in owner_admin_permissions if p is not PermissionKey.CUSTOMER_UPDATE
    )
    rows = [
        {"role_id": ROLE_IDS[role], "permission_id": PERMISSION_IDS[permission]}
        for role in (RoleKey.ORG_OWNER, RoleKey.ORG_ADMIN)
        for permission in owner_admin_permissions
    ] + [
        {"role_id": ROLE_IDS[RoleKey.ORG_MEMBER], "permission_id": PERMISSION_IDS[permission]}
        for permission in member_permissions
    ]
    op.bulk_insert(role_permissions_table, rows)


def downgrade() -> None:
    drop_tenant_rls(op, "conversation_activities")
    drop_tenant_rls(op, "conversation_participants")
    drop_tenant_rls(op, "conversations")
    drop_tenant_rls(op, "customer_identities")
    drop_tenant_rls(op, "customers")
    op.drop_table("conversation_activities")
    op.drop_table("conversation_participants")
    op.drop_table("conversations")
    op.drop_table("customer_identities")
    op.drop_table("customers")
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN ("
        "'b2000000-0000-7000-8000-00000000000a',"
        "'b2000000-0000-7000-8000-00000000000b',"
        "'b2000000-0000-7000-8000-00000000000c',"
        "'b2000000-0000-7000-8000-00000000000d',"
        "'b2000000-0000-7000-8000-00000000000e',"
        "'b2000000-0000-7000-8000-00000000000f',"
        "'b2000000-0000-7000-8000-000000000010',"
        "'b2000000-0000-7000-8000-000000000011')"
    )
    op.execute(
        "DELETE FROM permissions WHERE permission_key LIKE 'customer:%' "
        "OR permission_key LIKE 'conversation:%'"
    )
