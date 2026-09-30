"""Preserve the legacy chain and add fixed platform integrity domains."""

import sqlalchemy as sa
from alembic import op

revision = "d22c0b1c2d3e"
down_revision = "d22b0b1c2d3e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "platform_audit_intents",
        sa.Column("source_role", sa.String(64), nullable=False, server_default="nexus_sentinel"),
    )
    op.execute("""
        CREATE FUNCTION platform_audit_source_identity() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF current_user <> session_user OR
               coalesce(current_setting('nxs.organization_id',true),'') <> '' THEN
                RAISE EXCEPTION 'invalid platform source identity' USING ERRCODE='42501';
            END IF;
            NEW.source_role := session_user;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER platform_audit_source_identity BEFORE INSERT ON platform_audit_intents "
        "FOR EACH ROW EXECUTE FUNCTION platform_audit_source_identity()"
    )
    op.drop_constraint(
        op.f("ck_platform_audit_heads_singleton"), "platform_audit_heads", type_="check"
    )
    op.create_check_constraint(
        "domains", "platform_audit_heads", "id BETWEEN 1 AND 17 AND sequence >= 0"
    )
    op.execute(
        "INSERT INTO platform_audit_heads SELECT domain_id,0,repeat('0',64) "
        "FROM generate_series(2,17) domain_id"
    )
    op.add_column(
        "platform_audit_records",
        sa.Column("domain", sa.String(32), nullable=False, server_default="platform:v1:legacy"),
    )
    op.drop_constraint(
        op.f("uq_platform_audit_records_sequence"), "platform_audit_records", type_="unique"
    )
    op.create_unique_constraint(
        "uq_platform_audit_records_domain_sequence",
        "platform_audit_records",
        ["domain", "sequence"],
    )
    op.execute("GRANT UPDATE(id) ON platform_audit_intents TO nexus_audit_platform")
    op.create_check_constraint(
        "integrity_domain",
        "platform_audit_records",
        "((domain = 'platform:v1:legacy' AND fact->>'integrity_version' = '1') OR "
        "(domain ~ '^platform:v2:0[0-9a-f]$' AND fact->>'integrity_version' = '2' "
        "AND fact->>'domain' = domain)) IS TRUE",
    )


def downgrade() -> None:
    populated = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM platform_audit_records "
                "WHERE domain <> 'platform:v1:legacy') "
                "OR EXISTS(SELECT 1 FROM platform_audit_heads WHERE id <> 1 AND sequence <> 0)"
            )
        )
        .scalar_one()
    )
    if populated:
        raise RuntimeError("cannot collapse populated platform domains; retain audit records")
    op.execute("REVOKE UPDATE(id) ON platform_audit_intents FROM nexus_audit_platform")
    op.drop_constraint(
        op.f("ck_platform_audit_records_integrity_domain"), "platform_audit_records", type_="check"
    )
    op.drop_constraint(
        "uq_platform_audit_records_domain_sequence", "platform_audit_records", type_="unique"
    )
    op.create_unique_constraint(
        op.f("uq_platform_audit_records_sequence"), "platform_audit_records", ["sequence"]
    )
    op.drop_column("platform_audit_records", "domain")
    op.execute("DELETE FROM platform_audit_heads WHERE id <> 1")
    op.drop_constraint(
        op.f("ck_platform_audit_heads_domains"), "platform_audit_heads", type_="check"
    )
    op.create_check_constraint("singleton", "platform_audit_heads", "id = 1 AND sequence >= 0")
    op.execute("DROP TRIGGER platform_audit_source_identity ON platform_audit_intents")
    op.execute("DROP FUNCTION platform_audit_source_identity()")
    op.drop_column("platform_audit_intents", "source_role")
