"""Durable bounded edge authentication replay arbitration."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d19c0b1c2d3e"
down_revision: str | None = "d19b0b1c2d3e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table("sip_edge_auth_heads", sa.Column("edge_id", sa.Uuid(), primary_key=True))
    op.create_table(
        "sip_edge_replays",
        sa.Column(
            "edge_id",
            sa.Uuid(),
            sa.ForeignKey("sip_edge_auth_heads.edge_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("nonce_digest", sa.String(64), primary_key=True),
        sa.Column("boot_id", sa.Uuid(), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("nonce_digest ~ '^[a-f0-9]{64}$'", name="nonce_digest_valid"),
        sa.CheckConstraint("request_digest ~ '^[a-f0-9]{64}$'", name="request_digest_valid"),
    )
    op.create_index("ix_sip_edge_replays_expiry", "sip_edge_replays", ["edge_id", "expires_at"])
    condition = "edge_id = nullif(current_setting('nxs.sip_edge_id', true), '')::uuid"
    for table in ("sip_edge_auth_heads", "sip_edge_replays"):
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY edge_read ON "{table}" FOR SELECT TO nexus_runtime USING ({condition})'
        )
        op.execute(
            f'CREATE POLICY edge_insert ON "{table}" FOR INSERT TO nexus_runtime '
            f"WITH CHECK ({condition})"
        )
        op.execute(f'REVOKE ALL ON "{table}" FROM nexus_runtime')
        op.execute(f'GRANT SELECT, INSERT ON "{table}" TO nexus_runtime')
    op.execute("GRANT UPDATE ON sip_edge_auth_heads TO nexus_runtime")
    op.execute(
        "CREATE POLICY edge_lock ON sip_edge_auth_heads FOR UPDATE TO nexus_runtime "
        f"USING ({condition}) WITH CHECK ({condition})"
    )
    op.execute("GRANT DELETE ON sip_edge_replays TO nexus_runtime")
    op.execute(
        "CREATE POLICY edge_expire ON sip_edge_replays FOR DELETE TO nexus_runtime "
        f"USING ({condition} AND expires_at < clock_timestamp())"
    )


def downgrade() -> None:
    op.drop_table("sip_edge_replays")
    op.drop_table("sip_edge_auth_heads")
