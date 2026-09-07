"""organizations core (NXS-ORG-002, NXS-TENANT-003)

The first real domain table. Self-scoped: forced Row-Level Security binds every row to
the transaction-local ``nxs.organization_id`` on the primary key, and the non-bypass
``nexus_runtime`` role is granted only SELECT/INSERT/UPDATE — never DELETE.

Revision ID: ea956f004f7a
Revises:
Create Date: 2026-09-07 06:12:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from nexus_ai.infrastructure.rls import apply_tenant_rls, drop_tenant_rls

revision: str = "ea956f004f7a"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_key", sa.String(length=48), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("legal_name", sa.String(length=200), nullable=False),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("industry_code", sa.String(length=32), nullable=True),
        sa.Column("tax_identifier", sa.String(length=64), nullable=True),
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
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PROVISIONING', 'ACTIVE', 'SUSPENDED', 'ARCHIVED')",
            name=op.f("ck_organizations_status_known"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_organizations_version_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organizations")),
        sa.UniqueConstraint("organization_key", name="uq_organizations_organization_key"),
    )
    op.create_index("ix_organizations_status", "organizations", ["status"], unique=False)

    # Self-scoped tenant isolation: policy matches the primary key, not organization_id.
    apply_tenant_rls(op, "organizations", scope_column="id", allow_delete=False)


def downgrade() -> None:
    drop_tenant_rls(op, "organizations")
    op.drop_index("ix_organizations_status", table_name="organizations")
    op.drop_table("organizations")
