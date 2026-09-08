"""integration hub (NXS-INT-001)

The P07 Integration Hub schema. Every table is TENANT-OWNED with forced RLS
(``apply_tenant_rls``). Rows that reference another Hub table use COMPOSITE TENANT-AWARE
foreign keys ``(organization_id, <parent_id>) -> (organization_id, id)`` so the database
itself refuses a cross-tenant attachment (ADR-0052 pattern, ADR-0054). ``integration_secrets``
stores Fernet ciphertext only — no plaintext secret is ever written to PostgreSQL.

The migration also seeds the P07 permission-catalog delta: owner/admin receive every
``integration:*`` permission; org_member receives ``integration:read`` and
``integration:execute`` (invoke a configured integration, but never manage one).

Revision ID: 7b1c2d3e4f50
Revises: 6e63fd35017a
Create Date: 2026-09-08 03:57:55+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from nexus_ai.domain.auth.rbac import PERMISSION_IDS, ROLE_IDS, PermissionKey, RoleKey
from nexus_ai.infrastructure.rls import apply_tenant_rls, drop_tenant_rls

revision: str = "7b1c2d3e4f50"
down_revision: str | None = "6e63fd35017a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSONB = postgresql.JSONB(astext_type=sa.Text())
_NOW = sa.text("now()")

_P07_PERMISSIONS: tuple[tuple[PermissionKey, str], ...] = (
    (PermissionKey.INTEGRATION_READ, "Read integrations and their operations"),
    (PermissionKey.INTEGRATION_CREATE, "Create integrations"),
    (PermissionKey.INTEGRATION_UPDATE, "Update integration configuration (bumps the revision)"),
    (PermissionKey.INTEGRATION_DISABLE, "Enable / disable integrations"),
    (PermissionKey.INTEGRATION_OPERATION_MANAGE, "Register and remove integration operations"),
    (PermissionKey.INTEGRATION_CREDENTIAL_MANAGE, "Store and rotate integration credentials"),
    (PermissionKey.INTEGRATION_TEST, "Run a bounded connectivity test against an integration"),
    (PermissionKey.INTEGRATION_EXECUTE, "Invoke a configured integration operation"),
    (PermissionKey.INTEGRATION_WEBHOOK_MANAGE, "Register and manage inbound webhook endpoints"),
)
_MEMBER_PERMISSIONS = (PermissionKey.INTEGRATION_READ, PermissionKey.INTEGRATION_EXECUTE)


def _created_updated() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "integrations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("integration_type", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("base_url", sa.String(length=2048), nullable=False),
        sa.Column("config_revision", sa.Integer(), nullable=False),
        sa.Column("auth_profile", _JSONB, nullable=False),
        sa.Column("destination_rule", _JSONB, nullable=False),
        *_created_updated(),
        sa.CheckConstraint(
            "integration_type IN ('REST','OPENAPI','GRAPHQL','CRM','ERP','CALENDAR','WEBHOOK')",
            name=op.f("ck_integrations_integration_type_known"),
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT','ACTIVE','DISABLED','ERROR')",
            name=op.f("ck_integrations_integration_status_known"),
        ),
        sa.CheckConstraint(
            "config_revision >= 1", name=op.f("ck_integrations_config_revision_positive")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_integrations_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integrations")),
        sa.UniqueConstraint("organization_id", "id", name="uq_integrations_org_id"),
        sa.UniqueConstraint("organization_id", "slug", name="uq_integrations_org_slug"),
    )
    op.create_index(
        op.f("ix_integrations_organization_id"), "integrations", ["organization_id"], unique=False
    )
    op.create_index(
        "ix_integrations_status", "integrations", ["organization_id", "status"], unique=False
    )
    apply_tenant_rls(op, "integrations")

    op.create_table(
        "integration_secrets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("credential_ref", sa.String(length=128), nullable=False),
        sa.Column("credential_type", sa.String(length=16), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        *_created_updated(),
        sa.CheckConstraint(
            "credential_type IN "
            "('API_KEY','BEARER_TOKEN','BASIC_AUTH','OAUTH2_CLIENT','HMAC_SECRET')",
            name=op.f("ck_integration_secrets_integration_secret_type_known"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_integration_secrets_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integration_secrets")),
        sa.UniqueConstraint("organization_id", "credential_ref", name="uq_integration_secrets_ref"),
    )
    op.create_index(
        op.f("ix_integration_secrets_organization_id"),
        "integration_secrets",
        ["organization_id"],
        unique=False,
    )
    apply_tenant_rls(op, "integration_secrets", allow_delete=True)

    op.create_table(
        "integration_operations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("integration_id", sa.UUID(), nullable=False),
        sa.Column("operation_key", sa.String(length=64), nullable=False),
        sa.Column("operation_type", sa.String(length=16), nullable=False),
        sa.Column("spec", _JSONB, nullable=False),
        sa.Column("config_revision", sa.Integer(), nullable=False),
        *_created_updated(),
        sa.CheckConstraint(
            "operation_type IN ('REST','GRAPHQL')",
            name=op.f("ck_integration_operations_integration_operation_type_known"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "integration_id"],
            ["integrations.organization_id", "integrations.id"],
            name="fk_integration_operations_org_integration",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_integration_operations_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integration_operations")),
        sa.UniqueConstraint(
            "organization_id",
            "integration_id",
            "operation_key",
            name="uq_integration_operations_key",
        ),
    )
    op.create_index(
        "ix_integration_operations_integration",
        "integration_operations",
        ["organization_id", "integration_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_integration_operations_organization_id"),
        "integration_operations",
        ["organization_id"],
        unique=False,
    )
    apply_tenant_rls(op, "integration_operations", allow_delete=True)

    op.create_table(
        "integration_execution_records",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("integration_id", sa.UUID(), nullable=False),
        sa.Column("operation_key", sa.String(length=64), nullable=False),
        sa.Column("config_revision", sa.Integer(), nullable=False),
        sa.Column("result_class", sa.String(length=32), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("upstream_status", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "integration_id"],
            ["integrations.organization_id", "integrations.id"],
            name="fk_integration_execution_records_org_integration",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_integration_execution_records_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integration_execution_records")),
    )
    op.create_index(
        "ix_integration_execution_records_integration",
        "integration_execution_records",
        ["organization_id", "integration_id", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_integration_execution_records_organization_id"),
        "integration_execution_records",
        ["organization_id"],
        unique=False,
    )
    apply_tenant_rls(op, "integration_execution_records")

    op.create_table(
        "integration_idempotency_records",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("integration_id", sa.UUID(), nullable=False),
        sa.Column("operation_key", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("result_json", _JSONB, nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        *_created_updated(),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING','COMPLETED','FAILED')",
            name=op.f("ck_integration_idempotency_records_integration_idempotency_status_known"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "integration_id"],
            ["integrations.organization_id", "integrations.id"],
            name="fk_integration_idempotency_records_org_integration",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_integration_idempotency_records_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integration_idempotency_records")),
        sa.UniqueConstraint(
            "organization_id",
            "integration_id",
            "operation_key",
            "idempotency_key",
            name="uq_integration_idempotency_key",
        ),
    )
    op.create_index(
        "ix_integration_idempotency_expiry",
        "integration_idempotency_records",
        ["organization_id", "expires_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_integration_idempotency_records_organization_id"),
        "integration_idempotency_records",
        ["organization_id"],
        unique=False,
    )
    apply_tenant_rls(op, "integration_idempotency_records", allow_delete=True)

    op.create_table(
        "webhook_endpoints",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("integration_id", sa.UUID(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("public_token", sa.String(length=120), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("signature_scheme", sa.String(length=16), nullable=False),
        sa.Column("signature_header", sa.String(length=64), nullable=True),
        sa.Column("timestamp_header", sa.String(length=64), nullable=True),
        sa.Column("tolerance_seconds", sa.Integer(), nullable=False),
        sa.Column("max_body_bytes", sa.Integer(), nullable=False),
        sa.Column("credential_ref", sa.String(length=128), nullable=True),
        *_created_updated(),
        sa.CheckConstraint(
            "signature_scheme IN ('NONE','HMAC_SHA256')",
            name=op.f("ck_webhook_endpoints_webhook_signature_scheme_known"),
        ),
        sa.CheckConstraint(
            "tolerance_seconds BETWEEN 30 AND 3600",
            name=op.f("ck_webhook_endpoints_webhook_tolerance_bounds"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "integration_id"],
            ["integrations.organization_id", "integrations.id"],
            name="fk_webhook_endpoints_org_integration",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_webhook_endpoints_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_endpoints")),
        sa.UniqueConstraint("organization_id", "id", name="uq_webhook_endpoints_org_id"),
        sa.UniqueConstraint("organization_id", "slug", name="uq_webhook_endpoints_org_slug"),
        sa.UniqueConstraint(
            "organization_id", "public_token", name="uq_webhook_endpoints_public_token"
        ),
    )
    op.create_index(
        "ix_webhook_endpoints_integration",
        "webhook_endpoints",
        ["organization_id", "integration_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_webhook_endpoints_organization_id"),
        "webhook_endpoints",
        ["organization_id"],
        unique=False,
    )
    apply_tenant_rls(op, "webhook_endpoints")

    op.create_table(
        "webhook_receipts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("webhook_endpoint_id", sa.UUID(), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.CheckConstraint(
            "status IN ('RECEIVED','ACCEPTED','REJECTED')",
            name=op.f("ck_webhook_receipts_webhook_receipt_status_known"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "webhook_endpoint_id"],
            ["webhook_endpoints.organization_id", "webhook_endpoints.id"],
            name="fk_webhook_receipts_org_endpoint",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_webhook_receipts_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_receipts")),
        sa.UniqueConstraint(
            "organization_id",
            "webhook_endpoint_id",
            "external_id",
            name="uq_webhook_receipts_dedup",
        ),
    )
    op.create_index(
        "ix_webhook_receipts_endpoint",
        "webhook_receipts",
        ["organization_id", "webhook_endpoint_id", "received_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_webhook_receipts_organization_id"),
        "webhook_receipts",
        ["organization_id"],
        unique=False,
    )
    apply_tenant_rls(op, "webhook_receipts", allow_delete=True)

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
            {
                "id": PERMISSION_IDS[key],
                "permission_key": key.value,
                "description": description,
            }
            for key, description in _P07_PERMISSIONS
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
        for key, _ in _P07_PERMISSIONS
    ] + [
        {"role_id": ROLE_IDS[RoleKey.ORG_MEMBER], "permission_id": PERMISSION_IDS[key]}
        for key in _MEMBER_PERMISSIONS
    ]
    op.bulk_insert(role_permissions_table, rows)


def downgrade() -> None:
    permission_ids = [str(PERMISSION_IDS[key]) for key, _ in _P07_PERMISSIONS]
    op.execute(
        sa.text("DELETE FROM role_permissions WHERE permission_id = ANY(:ids)").bindparams(
            sa.bindparam("ids", value=permission_ids)
        )
    )
    op.execute("DELETE FROM permissions WHERE permission_key LIKE 'integration:%'")

    for table in (
        "webhook_receipts",
        "webhook_endpoints",
        "integration_idempotency_records",
        "integration_execution_records",
        "integration_operations",
        "integration_secrets",
        "integrations",
    ):
        drop_tenant_rls(op, table)
        op.drop_table(table)
