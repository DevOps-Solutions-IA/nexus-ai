"""make workflow dependency semantics explicit (NXS-P14 corrective #2)

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-09-13 12:45:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d6e7f8a9b0c1"
down_revision: str | None = "c5d6e7f8a9b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workflow_version_steps",
        sa.Column("dependency_mode", sa.String(length=8), server_default="ALL", nullable=False),
    )
    op.create_check_constraint(
        "ck_workflow_version_step_dependency_mode",
        "workflow_version_steps",
        "dependency_mode IN ('ALL','ANY')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_workflow_version_step_dependency_mode",
        "workflow_version_steps",
        type_="check",
    )
    op.drop_column("workflow_version_steps", "dependency_mode")
