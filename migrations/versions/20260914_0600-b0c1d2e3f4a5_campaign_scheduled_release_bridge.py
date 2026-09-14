"""campaign scheduled-release bridge (NXS-P16 corrective #2)

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
Create Date: 2026-09-14 06:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b0c1d2e3f4a5"
down_revision: str | None = "a9b0c1d2e3f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "campaign_runs",
        sa.Column("release_schedule_occurrence_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_campaign_runs_org_release_occurrence",
        "campaign_runs",
        "scheduler_occurrences",
        ["organization_id", "release_schedule_occurrence_id"],
        ["organization_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_campaign_runs_release_occurrence",
        "campaign_runs",
        ["organization_id", "release_schedule_occurrence_id"],
    )
    op.create_check_constraint(
        "ck_campaign_runs_release_binding",
        "campaign_runs",
        "release_schedule_occurrence_id IS NULL OR "
        "(schedule_id IS NOT NULL AND release_workflow_run_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_campaign_runs_release_binding", "campaign_runs", type_="check")
    op.drop_constraint("uq_campaign_runs_release_occurrence", "campaign_runs", type_="unique")
    op.drop_constraint(
        "fk_campaign_runs_org_release_occurrence", "campaign_runs", type_="foreignkey"
    )
    op.drop_column("campaign_runs", "release_schedule_occurrence_id")
