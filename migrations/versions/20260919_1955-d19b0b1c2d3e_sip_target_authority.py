"""Platform-controlled immutable SIP targets and mutation receipts."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d19b0b1c2d3e"
down_revision: str | None = "d19a0b1c2d3e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_platform_grants_capability_known"), "platform_grants", type_="check"
    )
    op.create_check_constraint(
        "capability_known",
        "platform_grants",
        "capability IN ('organization:create','cell:control','sip_edge:control')",
    )
    op.create_table(
        "cell_sip_targets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "cell_id", sa.Uuid(), sa.ForeignKey("cells.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("target_revision", sa.BigInteger(), nullable=False),
        sa.Column("host", sa.String(45), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("transport", sa.String(3), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("cell_id", "target_revision", name="uq_cell_sip_target_revision"),
        sa.UniqueConstraint("cell_id", "id", name="uq_cell_sip_target_identity"),
        sa.CheckConstraint("target_revision > 0", name="revision_positive"),
        sa.CheckConstraint("port BETWEEN 1024 AND 65535", name="port_bounded"),
        sa.CheckConstraint("transport IN ('UDP','TCP','TLS')", name="transport_known"),
        sa.CheckConstraint(
            "state IN ('REGISTERED','ACTIVE','DRAINING','RETIRED')", name="state_known"
        ),
    )
    op.create_index(
        "uq_cell_sip_target_active",
        "cell_sip_targets",
        ["cell_id"],
        unique=True,
        postgresql_where=sa.text("state = 'ACTIVE'"),
    )
    op.create_table(
        "cell_sip_target_heads",
        sa.Column(
            "cell_id", sa.Uuid(), sa.ForeignKey("cells.id", ondelete="RESTRICT"), primary_key=True
        ),
        sa.Column("control_revision", sa.BigInteger(), nullable=False),
        sa.Column("active_target_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint("control_revision >= 0", name="revision_nonnegative"),
        sa.ForeignKeyConstraint(
            ["cell_id", "active_target_id"],
            ["cell_sip_targets.cell_id", "cell_sip_targets.id"],
            ondelete="RESTRICT",
        ),
    )
    op.create_table(
        "sip_target_mutations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("cell_id", sa.Uuid(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("expected_revision", sa.BigInteger(), nullable=False),
        sa.Column("result_revision", sa.BigInteger(), nullable=False),
        sa.Column("result_state", sa.String(16), nullable=False),
        sa.Column(
            "actor_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("operation", "key_hash"),
        sa.CheckConstraint(
            "operation IN ('REGISTER','ACTIVE','DRAINING','RETIRED')", name="operation_known"
        ),
        sa.CheckConstraint("result_revision > expected_revision", name="revision_advanced"),
        sa.ForeignKeyConstraint(
            ["cell_id", "target_id"],
            ["cell_sip_targets.cell_id", "cell_sip_targets.id"],
            ondelete="RESTRICT",
        ),
    )
    control = (
        "EXISTS (SELECT 1 FROM platform_grants grant_row JOIN users actor "
        "ON actor.id = grant_row.user_id WHERE grant_row.user_id = "
        "nullif(current_setting('nxs.principal_id', true), '')::uuid "
        "AND grant_row.capability = 'sip_edge:control' AND actor.status = 'ACTIVE')"
    )
    for table in ("cell_sip_targets", "cell_sip_target_heads", "sip_target_mutations"):
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(f'CREATE POLICY sip_control_read ON "{table}" FOR SELECT USING (true)')
        op.execute(
            f'CREATE POLICY sip_control_insert ON "{table}" FOR INSERT WITH CHECK ({control})'
        )
        op.execute(f'GRANT SELECT, INSERT ON "{table}" TO nexus_runtime')
        if table != "sip_target_mutations":
            op.execute(
                f'CREATE POLICY sip_control_update ON "{table}" FOR UPDATE '
                f"USING ({control}) WITH CHECK ({control})"
            )
            op.execute(f'GRANT UPDATE ON "{table}" TO nexus_runtime')
        else:
            op.execute(f'REVOKE UPDATE ON "{table}" FROM nexus_runtime')
    op.execute("""
        CREATE FUNCTION nxs_sip_target_fence() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.state <> 'REGISTERED' THEN
                    RAISE EXCEPTION 'target must be registered' USING ERRCODE = '23514';
                END IF;
            ELSE
                IF (NEW.id, NEW.cell_id, NEW.target_revision, NEW.host, NEW.port, NEW.transport,
                    NEW.created_at) IS DISTINCT FROM
                   (OLD.id, OLD.cell_id, OLD.target_revision, OLD.host, OLD.port, OLD.transport,
                    OLD.created_at)
                   OR NOT ((OLD.state = 'REGISTERED' AND NEW.state IN ('ACTIVE','RETIRED'))
                       OR (OLD.state = 'ACTIVE' AND NEW.state = 'DRAINING')
                       OR (OLD.state = 'DRAINING' AND NEW.state = 'RETIRED')) THEN
                    RAISE EXCEPTION 'invalid target transition' USING ERRCODE = '23514';
                END IF;
            END IF;
            IF NOT (host(NEW.host::inet) = NEW.host AND
                (NEW.host::inet <<= '10.0.0.0/8'::inet
                 OR NEW.host::inet <<= '172.16.0.0/12'::inet
                 OR NEW.host::inet <<= '192.168.0.0/16'::inet
                 OR NEW.host::inet <<= 'fc00::/7'::inet)) THEN
                RAISE EXCEPTION 'invalid target address' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER sip_target_fence BEFORE INSERT OR UPDATE ON cell_sip_targets
        FOR EACH ROW EXECUTE FUNCTION nxs_sip_target_fence()
    """)
    op.execute("""
        CREATE FUNCTION nxs_sip_target_head_fence() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF NEW.cell_id <> OLD.cell_id OR NEW.control_revision <> OLD.control_revision + 1 THEN
                RAISE EXCEPTION 'invalid target control revision' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER sip_target_head_fence BEFORE UPDATE ON cell_sip_target_heads
        FOR EACH ROW EXECUTE FUNCTION nxs_sip_target_head_fence()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER sip_target_head_fence ON cell_sip_target_heads")
    op.execute("DROP FUNCTION nxs_sip_target_head_fence()")
    op.execute("DROP TRIGGER sip_target_fence ON cell_sip_targets")
    op.execute("DROP FUNCTION nxs_sip_target_fence()")
    op.drop_table("sip_target_mutations")
    op.drop_table("cell_sip_target_heads")
    op.drop_index("uq_cell_sip_target_active", table_name="cell_sip_targets")
    op.drop_table("cell_sip_targets")
    op.execute("DELETE FROM platform_grants WHERE capability = 'sip_edge:control'")
    op.drop_constraint(
        op.f("ck_platform_grants_capability_known"), "platform_grants", type_="check"
    )
    op.create_check_constraint(
        "capability_known",
        "platform_grants",
        "capability IN ('organization:create','cell:control')",
    )
