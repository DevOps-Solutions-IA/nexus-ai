"""Independent platform source journal and immutable ledger."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "d22b0b1c2d3e"
down_revision = "d22a0b1c2d3e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_platform_grants_capability_known"), "platform_grants", type_="check"
    )
    op.create_check_constraint(
        "capability_known",
        "platform_grants",
        "capability IN ('organization:create', 'cell:control', 'sip_edge:control', "
        "'sentinel:read', 'sentinel:triage', "
        "'sentinel:approve', 'sentinel:control', 'audit:platform:read', 'audit:platform:verify')",
    )
    op.create_table(
        "platform_audit_intents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("payload", JSONB(), nullable=False),
        sa.CheckConstraint("octet_length(payload::text) <= 8192", name="payload_bounded"),
        sa.CheckConstraint(
            "(payload->>'scope' = 'PLATFORM' AND "
            "payload->'organization_id' = 'null'::jsonb) IS TRUE",
            name="platform_scope",
        ),
    )
    op.create_table(
        "platform_audit_heads",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.CheckConstraint("id = 1 AND sequence >= 0", name="singleton"),
    )
    op.create_table(
        "platform_audit_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "source_id",
            sa.Uuid(),
            sa.ForeignKey("platform_audit_intents.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("sequence", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("semantic_digest", sa.String(64), nullable=False),
        sa.Column("predecessor", sa.String(64), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("fact", JSONB(), nullable=False),
        sa.CheckConstraint("sequence > 0", name="positive_sequence"),
        sa.CheckConstraint("octet_length(fact::text) <= 8192", name="fact_bounded"),
        sa.CheckConstraint(
            "(fact->>'scope' = 'PLATFORM' AND fact->'organization_id' = 'null'::jsonb AND "
            "fact->'intent'->>'scope' = 'PLATFORM' AND "
            "fact->'intent'->'organization_id' = 'null'::jsonb) IS TRUE",
            name="platform_scope",
        ),
    )
    op.create_table(
        "platform_audit_receipts",
        sa.Column(
            "source_id", sa.Uuid(), sa.ForeignKey("platform_audit_intents.id"), primary_key=True
        ),
        sa.Column(
            "record_id",
            sa.Uuid(),
            sa.ForeignKey("platform_audit_records.id"),
            nullable=True,
            unique=True,
        ),
        sa.Column("reason_code", sa.String(32), nullable=True),
        sa.CheckConstraint(
            "((record_id IS NOT NULL AND reason_code IS NULL) OR "
            "(record_id IS NULL AND reason_code = 'INVALID_INTENT')) IS TRUE",
            name="result_valid",
        ),
    )
    for table in (
        "platform_audit_intents",
        "platform_audit_heads",
        "platform_audit_records",
        "platform_audit_receipts",
    ):
        op.execute(
            f"REVOKE ALL ON {table} FROM PUBLIC, nexus_runtime, "
            "nexus_sentinel, nexus_audit_platform"
        )
        op.execute(f"GRANT SELECT ON {table} TO nexus_audit_platform")
    op.execute("GRANT INSERT ON platform_audit_intents TO nexus_sentinel")
    op.execute(
        "GRANT INSERT ON platform_audit_records, platform_audit_receipts TO nexus_audit_platform"
    )
    op.execute("GRANT UPDATE ON platform_audit_heads TO nexus_audit_platform")
    op.execute(
        "INSERT INTO platform_audit_heads (id, sequence, digest) VALUES (1,0,repeat('0',64))"
    )
    for table in ("platform_audit_intents", "platform_audit_records", "platform_audit_receipts"):
        op.execute(
            f"CREATE TRIGGER immutable_platform_audit BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION audit_immutable()"
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM platform_grants WHERE capability IN "
        "('audit:platform:read','audit:platform:verify')"
    )
    op.drop_constraint(
        op.f("ck_platform_grants_capability_known"), "platform_grants", type_="check"
    )
    op.create_check_constraint(
        "capability_known",
        "platform_grants",
        "capability IN ('organization:create', 'cell:control', 'sip_edge:control', "
        "'sentinel:read', 'sentinel:triage', "
        "'sentinel:approve', 'sentinel:control')",
    )
    for table in (
        "platform_audit_receipts",
        "platform_audit_records",
        "platform_audit_heads",
        "platform_audit_intents",
    ):
        op.drop_table(table)
