"""Immutable Organization audit chains and narrow read capabilities."""

import sqlalchemy as sa
from alembic import op

from nexus_ai.infrastructure.rls import apply_tenant_rls

revision = "d22a0b1c2d3e"
down_revision = "d21a0b1c2d3e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_heads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id"),
        sa.CheckConstraint("id = organization_id", name="tenant_identity_valid"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("sequence >= 0", name="sequence_valid"),
        sa.CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="digest_valid"),
    )
    op.create_index("ix_audit_heads_organization_id", "audit_heads", ["organization_id"])
    op.create_table(
        "audit_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("producer", sa.String(32), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("original_record_id", sa.Uuid(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("semantic_digest", sa.String(64), nullable=False),
        sa.Column("predecessor", sa.String(64), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("fact", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "sequence", name="uq_audit_records_sequence"),
        sa.UniqueConstraint(
            "organization_id", "producer", "source_id", name="uq_audit_records_source"
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["organization_id", "original_record_id"],
            ["audit_records.organization_id", "audit_records.id"],
        ),
        sa.CheckConstraint("sequence > 0", name="sequence_valid"),
        sa.CheckConstraint(
            "digest ~ '^[0-9a-f]{64}$' AND predecessor ~ '^[0-9a-f]{64}$' "
            "AND semantic_digest ~ '^[0-9a-f]{64}$'",
            name="digests_valid",
        ),
        sa.CheckConstraint("octet_length(fact::text) <= 8192", name="fact_bounded"),
    )
    op.create_index("ix_audit_records_organization_id", "audit_records", ["organization_id"])
    for table in ("audit_heads", "audit_records"):
        apply_tenant_rls(op, table)
    op.execute("REVOKE UPDATE, DELETE ON audit_records FROM nexus_runtime")
    op.execute("""
        CREATE FUNCTION audit_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'immutable audit record' USING ERRCODE = '23514';
        END $$;
    """)
    op.execute(
        "CREATE TRIGGER immutable_audit BEFORE UPDATE OR DELETE ON audit_records "
        "FOR EACH ROW EXECUTE FUNCTION audit_immutable()"
    )
    for suffix, capability in enumerate(("read", "verify"), start=81):
        identity = f"b2000000-0000-7000-8000-{suffix:012x}"
        op.get_bind().execute(
            sa.text(
                "INSERT INTO permissions (id,permission_key,description,created_at) "
                "VALUES (:id,:key,'Tenant audit capability',now())"
            ),
            {"id": identity, "key": f"audit:{capability}"},
        )
        op.get_bind().execute(
            sa.text(
                "INSERT INTO role_permissions(role_id,permission_id,created_at) "
                "VALUES ('a1000000-0000-7000-8000-000000000001',:id,now())"
            ),
            {"id": identity},
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "(SELECT id FROM permissions WHERE permission_key IN ('audit:read','audit:verify'))"
    )
    op.execute("DELETE FROM permissions WHERE permission_key IN ('audit:read','audit:verify')")
    op.drop_table("audit_records")
    op.drop_table("audit_heads")
    op.execute("DROP FUNCTION audit_immutable()")
