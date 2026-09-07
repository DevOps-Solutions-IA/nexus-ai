"""auth foundation (NXS-AUTH-001..006)

The P03 security schema. Table classification is conscious and documented (ADR-0038):

GLOBAL identity plane — no RLS, runtime role gets only SELECT/INSERT/UPDATE:
* users, user_credentials — per-user identity and Argon2id credential history
* roles, permissions, role_permissions — platform catalogs (seeded below)

TENANT-OWNED — forced RLS bound to the transaction-local nxs.organization_id:
* memberships — one row per (organization, user); plus a self-visibility policy
  (USING only) that exposes a caller's own rows to the authenticated principal when
  no Organization scope is bound — the identity-plane login path
* role_assignments — organization-scoped RBAC source of truth
* refresh_sessions — server-side refresh state; only SHA-256 hashes are stored

Revision ID: 36741ae62327
Revises: ea956f004f7a
Create Date: 2026-09-07 09:15:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from nexus_ai.domain.auth.rbac import (
    PERMISSION_IDS,
    ROLE_IDS,
    ROLE_PERMISSIONS,
    PermissionKey,
    RoleKey,
)
from nexus_ai.infrastructure.rls import DEFAULT_RUNTIME_ROLE, apply_tenant_rls, drop_tenant_rls

revision: str = "36741ae62327"
down_revision: str | None = "ea956f004f7a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PRINCIPAL_SELF_POLICY = (
    "user_id = nullif(current_setting('nxs.principal_id', true), '')::uuid "
    "AND nullif(current_setting('nxs.organization_id', true), '') IS NULL"
)


def _grant_select_insert_update(table: str) -> None:
    op.execute(f'GRANT SELECT, INSERT, UPDATE ON "{table}" TO "{DEFAULT_RUNTIME_ROLE}"')


def upgrade() -> None:
    # --- global identity plane -------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("email_verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
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
        sa.CheckConstraint("status IN ('ACTIVE', 'SUSPENDED')", name=op.f("ck_users_status_known")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_users_version_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_status", "users", ["status"], unique=False)
    _grant_select_insert_update("users")

    op.create_table(
        "user_credentials",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("algorithm", sa.String(length=32), nullable=False),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("password_hash", sa.String(length=256), nullable=False),
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
            "credential_version >= 1", name=op.f("ck_user_credentials_credential_version_positive")
        ),
        sa.CheckConstraint(
            "algorithm = 'argon2id'", name=op.f("ck_user_credentials_algorithm_known")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_credentials_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("user_id", "credential_version", name=op.f("pk_user_credentials")),
    )
    _grant_select_insert_update("user_credentials")

    op.create_table(
        "roles",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("role_key", sa.String(length=48), nullable=False),
        sa.Column("description", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_roles")),
        sa.UniqueConstraint("role_key", name="uq_roles_role_key"),
    )
    _grant_select_insert_update("roles")

    op.create_table(
        "permissions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("permission_key", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_permissions")),
        sa.UniqueConstraint("permission_key", name="uq_permissions_permission_key"),
    )
    _grant_select_insert_update("permissions")

    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.UUID(), nullable=False),
        sa.Column("permission_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["roles.id"],
            name=op.f("fk_role_permissions_role_id_roles"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["permission_id"],
            ["permissions.id"],
            name=op.f("fk_role_permissions_permission_id_permissions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("role_id", "permission_id", name=op.f("pk_role_permissions")),
    )
    _grant_select_insert_update("role_permissions")

    # --- tenant-owned tables ---------------------------------------------------------
    op.create_table(
        "memberships",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
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
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'SUSPENDED', 'REVOKED')", name=op.f("ck_memberships_status_known")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_memberships_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_memberships_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memberships")),
        sa.UniqueConstraint("organization_id", "user_id", name="uq_memberships_org_user"),
    )
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"], unique=False)
    op.create_index(
        "ix_memberships_organization_id", "memberships", ["organization_id"], unique=False
    )
    apply_tenant_rls(op, "memberships")
    op.execute(
        f'CREATE POLICY "nxs_principal_self" ON "memberships" USING ({_PRINCIPAL_SELF_POLICY})'
    )

    op.create_table(
        "role_assignments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("role_id", sa.UUID(), nullable=False),
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
            "status IN ('ACTIVE', 'SUSPENDED')", name=op.f("ck_role_assignments_status_known")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_role_assignments_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_role_assignments_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["roles.id"],
            name=op.f("fk_role_assignments_role_id_roles"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_role_assignments")),
        sa.UniqueConstraint(
            "organization_id", "user_id", "role_id", name="uq_role_assignments_org_user_role"
        ),
    )
    op.create_index("ix_role_assignments_user_id", "role_assignments", ["user_id"], unique=False)
    op.create_index(
        "ix_role_assignments_organization_id", "role_assignments", ["organization_id"], unique=False
    )
    apply_tenant_rls(op, "role_assignments")

    op.create_table(
        "refresh_sessions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("previous_token_hash", sa.String(length=64), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("generation >= 1", name=op.f("ck_refresh_sessions_generation_positive")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_refresh_sessions_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_refresh_sessions_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refresh_sessions")),
        sa.UniqueConstraint("token_hash", name="uq_refresh_sessions_token_hash"),
    )
    op.create_index("ix_refresh_sessions_user_id", "refresh_sessions", ["user_id"], unique=False)
    op.create_index(
        "ix_refresh_sessions_organization_id", "refresh_sessions", ["organization_id"], unique=False
    )
    apply_tenant_rls(op, "refresh_sessions")

    # --- role / permission catalog seed (deterministic ids) ---------------------------
    roles_table = sa.table(
        "roles",
        sa.column("id", sa.UUID()),
        sa.column("role_key", sa.String()),
        sa.column("description", sa.String()),
    )
    op.bulk_insert(
        roles_table,
        [
            {
                "id": ROLE_IDS[RoleKey.ORG_OWNER],
                "role_key": "org_owner",
                "description": "Full control of the Organization including role assignments",
            },
            {
                "id": ROLE_IDS[RoleKey.ORG_ADMIN],
                "role_key": "org_admin",
                "description": "Manages the Organization and its members",
            },
            {
                "id": ROLE_IDS[RoleKey.ORG_MEMBER],
                "role_key": "org_member",
                "description": "Reads the Organization and its own session data",
            },
        ],
    )
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
                "id": PERMISSION_IDS[PermissionKey.ORGANIZATION_READ],
                "permission_key": "organization:read",
                "description": "Read the Organization profile",
            },
            {
                "id": PERMISSION_IDS[PermissionKey.ORGANIZATION_WRITE],
                "permission_key": "organization:write",
                "description": "Update the Organization profile",
            },
            {
                "id": PERMISSION_IDS[PermissionKey.MEMBERSHIP_MANAGE],
                "permission_key": "membership:manage",
                "description": "Invite, suspend and revoke members",
            },
            {
                "id": PERMISSION_IDS[PermissionKey.ROLE_ASSIGN],
                "permission_key": "role:assign",
                "description": "Assign and suspend roles",
            },
            {
                "id": PERMISSION_IDS[PermissionKey.SESSION_READ],
                "permission_key": "auth:session:read",
                "description": "Read the caller's own session and identity",
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
        for role, permissions in ROLE_PERMISSIONS.items()
        for permission in permissions
    ]
    op.bulk_insert(role_permissions_table, rows)


def downgrade() -> None:
    drop_tenant_rls(op, "refresh_sessions")
    drop_tenant_rls(op, "role_assignments")
    drop_tenant_rls(op, "memberships")
    op.execute('DROP POLICY IF EXISTS "nxs_principal_self" ON "memberships"')

    for table in ("refresh_sessions", "role_assignments", "memberships", "role_permissions"):
        op.drop_table(table)
    op.drop_table("permissions")
    op.drop_table("roles")
    op.drop_table("user_credentials")
    op.drop_table("users")
