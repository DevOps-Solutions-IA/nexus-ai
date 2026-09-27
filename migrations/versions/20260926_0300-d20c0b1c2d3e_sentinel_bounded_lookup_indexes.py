"""Bound active execution cleanup and latest incident observation lookup."""

from alembic import op

revision = "d20c0b1c2d3e"
down_revision = "d20b0b1c2d3e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_sentinel_execution_expiry ON sentinel_executions "
        "(lease_expires_at, proposal_id) WHERE dispatch_state IN ('CLAIMED','DISPATCHED')"
    )
    op.execute(
        "CREATE INDEX ix_sentinel_receipts_latest ON sentinel_signal_receipts "
        "(incident_id, received_at, id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_sentinel_receipts_latest")
    op.execute("DROP INDEX ix_sentinel_execution_expiry")
