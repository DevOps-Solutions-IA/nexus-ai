"""provisioning foundation (NXS-ORG-001, NXS-DASH-001)

The P05 provisioning schema. Classification is conscious and documented (ADR-0050):

GLOBAL — no RLS (the control plane runs before any tenant scope exists):
* provisioning_requests — durable idempotency ledger + workflow state; stores only
  SHA-256 hashes of idempotency keys and request fingerprints, never payload dumps.
* platform_grants — explicit user-scoped platform capabilities
  (organization:create). Organization role assignments NEVER grant platform
  capabilities and vice versa.

TENANT-OWNED — forced RLS bound to nxs.organization_id:
* organization_settings — one provisioning-created settings row per Organization.
* dashboard_configurations — provisioned baseline dashboard schema snapshots, one
  canonical row per (organization, revision).

The migration also extends the P03 permission catalog with the P05 permissions and
their role grants (org_owner/org_admin/org_member). ``organization:create`` is
registered in the catalog but granted through platform_grants only — no role ever
holds it.

Revision ID: 04a4640c5b95
Revises: 9387c6f0c917
Create Date: 2026-09-07 19:35:00+00:00
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
from nexus_ai.infrastructure.rls import DEFAULT_RUNTIME_ROLE, apply_tenant_rls, drop_tenant_rls

revision: str = "04a4640c5b95"
down_revision: str | None = "9387c6f0c917"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _grant_select_insert_update(table: str) -> None:
    op.execute(f'GRANT SELECT, INSERT, UPDATE ON "{table}" TO "{DEFAULT_RUNTIME_ROLE}"')


def upgrade() -> None:
    # --- global control plane ---------------------------------------------------------
    op.create_table(
        "provisioning_requests",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=True),
        sa.Column("organization_key", sa.String(length=48), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("request_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("owner_user_id", sa.UUID(), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=False),
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
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING', 'COMPLETED', 'FAILED')",
            name=op.f("ck_provisioning_requests_status_known"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_provisioning_requests_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_provisioning_requests_owner_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_provisioning_requests_created_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provisioning_requests")),
        sa.UniqueConstraint("idempotency_key_hash", name="uq_provisioning_requests_key_hash"),
    )
    op.create_index(
        "ix_provisioning_requests_organization_id",
        "provisioning_requests",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_provisioning_requests_organization_key",
        "provisioning_requests",
        ["organization_key"],
        unique=False,
    )
    _grant_select_insert_update("provisioning_requests")

    op.create_table(
        "platform_grants",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("capability", sa.String(length=64), nullable=False),
        sa.Column("granted_by_user_id", sa.UUID(), nullable=True),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "capability IN ('organization:create')",
            name=op.f("ck_platform_grants_capability_known"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_platform_grants_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_user_id"],
            ["users.id"],
            name=op.f("fk_platform_grants_granted_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("user_id", "capability", name=op.f("pk_platform_grants")),
    )
    _grant_select_insert_update("platform_grants")

    # --- tenant-owned tables ---------------------------------------------------------
    op.create_table(
        "organization_settings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("locale", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
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
            "revision >= 1", name=op.f("ck_organization_settings_revision_positive")
        ),
        sa.CheckConstraint(
            "length(locale) >= 2", name=op.f("ck_organization_settings_locale_bounded")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_organization_settings_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization_settings")),
        sa.UniqueConstraint("organization_id", name="uq_organization_settings_organization"),
    )
    op.create_index(
        "ix_organization_settings_organization_id",
        "organization_settings",
        ["organization_id"],
        unique=False,
    )
    apply_tenant_rls(op, "organization_settings")

    op.create_table(
        "dashboard_configurations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("configuration", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
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
            "schema_version >= 1", name=op.f("ck_dashboard_configurations_schema_version_positive")
        ),
        sa.CheckConstraint(
            "revision >= 1", name=op.f("ck_dashboard_configurations_revision_positive")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_dashboard_configurations_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dashboard_configurations")),
        sa.UniqueConstraint("organization_id", "revision", name="uq_dashboard_config_org_revision"),
    )
    op.create_index(
        "ix_dashboard_config_org_revision",
        "dashboard_configurations",
        ["organization_id", "revision"],
        unique=False,
    )
    op.create_index(
        "ix_dashboard_configurations_organization_id",
        "dashboard_configurations",
        ["organization_id"],
        unique=False,
    )
    apply_tenant_rls(op, "dashboard_configurations")

    # --- P05 permission catalog seed (deterministic ids) ------------------------------
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
                "id": PERMISSION_IDS[PermissionKey.ORGANIZATION_CREATE],
                "permission_key": "organization:create",
                "description": (
                    "Create a new Organization (platform capability, granted through "
                    "platform_grants)"
                ),
            },
            {
                "id": PERMISSION_IDS[PermissionKey.PROVISION_READ],
                "permission_key": "organization:provision:read",
                "description": "Read the Organization's provisioning workflow state",
            },
            {
                "id": PERMISSION_IDS[PermissionKey.SETTINGS_READ],
                "permission_key": "organization:settings:read",
                "description": "Read the Organization's platform settings",
            },
            {
                "id": PERMISSION_IDS[PermissionKey.DASHBOARD_READ],
                "permission_key": "dashboard:read",
                "description": "Read the Organization's dashboard schema (permission-filtered)",
            },
        ],
    )
    role_permissions_table = sa.table(
        "role_permissions",
        sa.column("role_id", sa.UUID()),
        sa.column("permission_id", sa.UUID()),
    )
    rows = [
        {"role_id": ROLE_IDS[role], "permission_id": PERMISSION_IDS[permission]}
        for role in (RoleKey.ORG_OWNER, RoleKey.ORG_ADMIN, RoleKey.ORG_MEMBER)
        for permission in (
            PermissionKey.PROVISION_READ,
            PermissionKey.SETTINGS_READ,
            PermissionKey.DASHBOARD_READ,
        )
    ]
    op.bulk_insert(role_permissions_table, rows)


def downgrade() -> None:
    drop_tenant_rls(op, "dashboard_configurations")
    drop_tenant_rls(op, "organization_settings")
    op.drop_table("dashboard_configurations")
    op.drop_table("organization_settings")
    op.drop_table("platform_grants")
    op.drop_table("provisioning_requests")
    # Remove the seeded P05 permissions and their role grants.
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN ("
        "'b2000000-0000-7000-8000-000000000006',"
        "'b2000000-0000-7000-8000-000000000007',"
        "'b2000000-0000-7000-8000-000000000008',"
        "'b2000000-0000-7000-8000-000000000009')"
    )
    op.execute(
        "DELETE FROM permissions WHERE permission_key IN ("
        "'organization:create', 'organization:provision:read', "
        "'organization:settings:read', 'dashboard:read')"
    )
