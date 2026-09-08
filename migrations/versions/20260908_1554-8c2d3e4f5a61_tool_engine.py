"""tool engine (NXS-TOOL-001)

The P08 Tool Engine schema. Every table is TENANT-OWNED with forced RLS. A
``tool_definitions`` row references the integration it is bound to with a COMPOSITE
TENANT-AWARE foreign key ``(organization_id, integration_id) -> (organization_id, id)`` on
``integrations``, so a tool can only bind an integration in its own Organization
(ADR-0052 pattern, ADR-0061). Stored schemas are bounded JSONB — never executable code.

The migration also seeds the P08 permission-catalog delta: owner/admin receive every
``tool:*`` permission; org_member receives ``tool:read`` and ``tool:invoke`` (invoke a
configured tool, but never manage one).

Revision ID: 8c2d3e4f5a61
Revises: 7b1c2d3e4f50
Create Date: 2026-09-08 15:54:11+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from nexus_ai.domain.auth.rbac import PERMISSION_IDS, ROLE_IDS, PermissionKey, RoleKey
from nexus_ai.infrastructure.rls import apply_tenant_rls, drop_tenant_rls

revision: str = "8c2d3e4f5a61"
down_revision: str | None = "7b1c2d3e4f50"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSONB = postgresql.JSONB(astext_type=sa.Text())
_NOW = sa.text("now()")

_P08_PERMISSIONS: tuple[tuple[PermissionKey, str], ...] = (
    (PermissionKey.TOOL_READ, "Read tool definitions"),
    (PermissionKey.TOOL_CREATE, "Register tool definitions"),
    (PermissionKey.TOOL_UPDATE, "Update tool definitions (bumps the version)"),
    (PermissionKey.TOOL_DISABLE, "Enable / disable tool definitions"),
    (PermissionKey.TOOL_INVOKE, "Invoke a configured tool"),
)
_MEMBER_PERMISSIONS = (PermissionKey.TOOL_READ, PermissionKey.TOOL_INVOKE)


def _created_updated() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "tool_definitions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("tool_key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=800), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("risk_class", sa.String(length=16), nullable=False),
        sa.Column("side_effect_class", sa.String(length=24), nullable=False),
        sa.Column("idempotency_policy", sa.String(length=16), nullable=False),
        sa.Column("timeout_seconds", sa.Float(), nullable=True),
        sa.Column("input_schema", _JSONB, nullable=False),
        sa.Column("output_schema", _JSONB, nullable=True),
        sa.Column("required_permissions", _JSONB, nullable=False),
        sa.Column("binding_type", sa.String(length=16), nullable=False),
        sa.Column("integration_id", sa.UUID(), nullable=True),
        sa.Column("operation_key", sa.String(length=64), nullable=True),
        sa.Column("static_arguments", _JSONB, nullable=False),
        *_created_updated(),
        sa.CheckConstraint(
            "binding_type <> 'INTEGRATION' OR "
            "(integration_id IS NOT NULL AND operation_key IS NOT NULL)",
            name=op.f("ck_tool_definitions_tool_integration_binding_complete"),
        ),
        sa.CheckConstraint(
            "binding_type IN ('INTEGRATION')",
            name=op.f("ck_tool_definitions_tool_binding_type_known"),
        ),
        sa.CheckConstraint(
            "idempotency_policy IN ('NONE','OPTIONAL','REQUIRED')",
            name=op.f("ck_tool_definitions_tool_idempotency_policy_known"),
        ),
        sa.CheckConstraint(
            "risk_class IN ('LOW','MEDIUM','HIGH','CRITICAL')",
            name=op.f("ck_tool_definitions_tool_risk_class_known"),
        ),
        sa.CheckConstraint(
            "side_effect_class IN "
            "('READ_ONLY','IDEMPOTENT_WRITE','NON_IDEMPOTENT_WRITE','EXTERNAL_EFFECT')",
            name=op.f("ck_tool_definitions_tool_side_effect_class_known"),
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT','ACTIVE','DISABLED','ERROR')",
            name=op.f("ck_tool_definitions_tool_status_known"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_tool_definitions_tool_version_positive")),
        sa.ForeignKeyConstraint(
            ["organization_id", "integration_id"],
            ["integrations.organization_id", "integrations.id"],
            name="fk_tool_definitions_org_integration",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_tool_definitions_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_definitions")),
        sa.UniqueConstraint("organization_id", "id", name="uq_tool_definitions_org_id"),
        sa.UniqueConstraint("organization_id", "tool_key", name="uq_tool_definitions_org_key"),
    )
    op.create_index(
        op.f("ix_tool_definitions_organization_id"),
        "tool_definitions",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_tool_definitions_status",
        "tool_definitions",
        ["organization_id", "status"],
        unique=False,
    )
    apply_tenant_rls(op, "tool_definitions", allow_delete=True)

    op.create_table(
        "tool_execution_records",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("tool_id", sa.UUID(), nullable=False),
        sa.Column("tool_key", sa.String(length=64), nullable=False),
        sa.Column("tool_version", sa.Integer(), nullable=False),
        sa.Column("result_class", sa.String(length=32), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("downstream_code", sa.String(length=64), nullable=True),
        sa.Column("downstream_status", sa.Integer(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("caller_user_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.CheckConstraint(
            "retry_count >= 0",
            name=op.f("ck_tool_execution_records_tool_execution_retry_nonneg"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "tool_id"],
            ["tool_definitions.organization_id", "tool_definitions.id"],
            name="fk_tool_execution_records_org_tool",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_tool_execution_records_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_execution_records")),
    )
    op.create_index(
        op.f("ix_tool_execution_records_organization_id"),
        "tool_execution_records",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_tool_execution_records_tool",
        "tool_execution_records",
        ["organization_id", "tool_id", "created_at"],
        unique=False,
    )
    apply_tenant_rls(op, "tool_execution_records")

    op.create_table(
        "tool_idempotency_records",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("tool_id", sa.UUID(), nullable=False),
        sa.Column("tool_key", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("result_json", _JSONB, nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        *_created_updated(),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING','COMPLETED','FAILED')",
            name=op.f("ck_tool_idempotency_records_tool_idempotency_status_known"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "tool_id"],
            ["tool_definitions.organization_id", "tool_definitions.id"],
            name="fk_tool_idempotency_records_org_tool",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_tool_idempotency_records_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_idempotency_records")),
        sa.UniqueConstraint(
            "organization_id", "tool_id", "idempotency_key", name="uq_tool_idempotency_key"
        ),
    )
    op.create_index(
        "ix_tool_idempotency_expiry",
        "tool_idempotency_records",
        ["organization_id", "expires_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_tool_idempotency_records_organization_id"),
        "tool_idempotency_records",
        ["organization_id"],
        unique=False,
    )
    apply_tenant_rls(op, "tool_idempotency_records", allow_delete=True)

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
            for key, description in _P08_PERMISSIONS
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
        for key, _ in _P08_PERMISSIONS
    ] + [
        {"role_id": ROLE_IDS[RoleKey.ORG_MEMBER], "permission_id": PERMISSION_IDS[key]}
        for key in _MEMBER_PERMISSIONS
    ]
    op.bulk_insert(role_permissions_table, rows)


def downgrade() -> None:
    permission_ids = [str(PERMISSION_IDS[key]) for key, _ in _P08_PERMISSIONS]
    op.execute(
        sa.text("DELETE FROM role_permissions WHERE permission_id = ANY(:ids)").bindparams(
            sa.bindparam("ids", value=permission_ids)
        )
    )
    op.execute("DELETE FROM permissions WHERE permission_key LIKE 'tool:%'")

    for table in (
        "tool_idempotency_records",
        "tool_execution_records",
        "tool_definitions",
    ):
        drop_tenant_rls(op, table)
        op.drop_table(table)
