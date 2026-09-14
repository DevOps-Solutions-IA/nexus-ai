"""campaign execution correctness corrective (NXS-P16 corrective #1)

Revision ID: a9b0c1d2e3f4
Revises: f8a9b0c1d2e3
Create Date: 2026-09-14 04:15:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from nexus_ai.infrastructure.rls import apply_tenant_rls, drop_tenant_rls

revision: str = "a9b0c1d2e3f4"
down_revision: str | None = "f8a9b0c1d2e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_campaigns_state", "campaigns", type_="check")
    op.create_check_constraint(
        "ck_campaigns_state",
        "campaigns",
        "state IN ('DRAFT','PREPARING','READY','SCHEDULED','RUNNING','PAUSED',"
        "'CANCELLING','COMPLETED','FAILED','CANCELLED')",
    )
    op.drop_constraint("ck_campaign_runs_state", "campaign_runs", type_="check")
    op.create_check_constraint(
        "ck_campaign_runs_state",
        "campaign_runs",
        "state IN ('PENDING_RELEASE','MATERIALIZING','RUNNING','PAUSED','CANCELLING',"
        "'COMPLETED','FAILED','CANCELLED')",
    )
    op.add_column(
        "campaign_runs",
        sa.Column("authorized_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "campaign_runs",
        sa.Column(
            "attempt_materialization_cursor", sa.Integer(), server_default="0", nullable=False
        ),
    )
    op.add_column(
        "campaign_runs",
        sa.Column(
            "attempt_materialization_complete", sa.Boolean(), server_default="false", nullable=False
        ),
    )
    op.add_column(
        "campaign_runs",
        sa.Column("cancellation_processed_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "campaign_runs",
        sa.Column("cancellation_complete", sa.Boolean(), server_default="false", nullable=False),
    )
    op.create_check_constraint(
        "ck_campaign_runs_progress",
        "campaign_runs",
        "authorized_count >= 0 AND attempt_materialization_cursor >= 0 "
        "AND cancellation_processed_count >= 0",
    )
    op.create_unique_constraint(
        "uq_campaign_runs_schedule", "campaign_runs", ["organization_id", "schedule_id"]
    )
    op.create_table(
        "campaign_organization_throttle_windows",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reserved_count", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_campaign_organization_throttle_windows_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_campaign_organization_throttle_windows")),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_campaign_org_throttle_windows_org_id"
        ),
        sa.UniqueConstraint(
            "organization_id", "channel", "window_start", name="uq_campaign_org_throttle_window"
        ),
        sa.CheckConstraint("reserved_count >= 0", name="ck_campaign_org_throttle_count"),
    )
    op.create_index(
        op.f("ix_campaign_organization_throttle_windows_organization_id"),
        "campaign_organization_throttle_windows",
        ["organization_id"],
    )
    apply_tenant_rls(op, "campaign_organization_throttle_windows")


def downgrade() -> None:
    drop_tenant_rls(op, "campaign_organization_throttle_windows")
    op.drop_index(
        op.f("ix_campaign_organization_throttle_windows_organization_id"),
        table_name="campaign_organization_throttle_windows",
    )
    op.drop_table("campaign_organization_throttle_windows")
    op.drop_constraint("uq_campaign_runs_schedule", "campaign_runs", type_="unique")
    op.drop_constraint("ck_campaign_runs_progress", "campaign_runs", type_="check")
    for column in (
        "cancellation_complete",
        "cancellation_processed_count",
        "attempt_materialization_complete",
        "attempt_materialization_cursor",
        "authorized_count",
    ):
        op.drop_column("campaign_runs", column)
    op.drop_constraint("ck_campaign_runs_state", "campaign_runs", type_="check")
    op.create_check_constraint(
        "ck_campaign_runs_state",
        "campaign_runs",
        "state IN ('PENDING_RELEASE','RUNNING','PAUSED','COMPLETED','FAILED','CANCELLED')",
    )
    op.drop_constraint("ck_campaigns_state", "campaigns", type_="check")
    op.create_check_constraint(
        "ck_campaigns_state",
        "campaigns",
        "state IN ('DRAFT','PREPARING','READY','SCHEDULED','RUNNING','PAUSED',"
        "'COMPLETED','FAILED','CANCELLED')",
    )
