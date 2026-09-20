"""Immutable upstream revisions and one durable egress permit per P11 call."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from nexus_ai.infrastructure.rls import apply_tenant_rls

revision: str = "d19e0b1c2d3e"
down_revision: str | None = "d19d0b1c2d3e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sip_upstreams",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("revision", sa.BigInteger(), primary_key=True),
        sa.Column("host", sa.String(45), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("transport", sa.String(3), nullable=False),
        sa.Column(
            "cell_id", sa.Uuid(), sa.ForeignKey("cells.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("asterisk_peer_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("revision > 0", name="revision_positive"),
        sa.CheckConstraint("port BETWEEN 1024 AND 65535", name="port_bounded"),
        sa.CheckConstraint("transport IN ('UDP','TCP','TLS')", name="transport_known"),
    )
    control = (
        "EXISTS (SELECT 1 FROM platform_grants grant_row JOIN users actor "
        "ON actor.id = grant_row.user_id WHERE grant_row.user_id = "
        "nullif(current_setting('nxs.principal_id', true), '')::uuid "
        "AND grant_row.capability = 'sip_edge:control' AND actor.status = 'ACTIVE')"
    )
    op.execute("ALTER TABLE sip_upstreams ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE sip_upstreams FORCE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON sip_upstreams FROM nexus_runtime")
    op.execute("GRANT SELECT, INSERT, UPDATE ON sip_upstreams TO nexus_runtime")
    op.execute("CREATE POLICY upstream_read ON sip_upstreams FOR SELECT USING (true)")
    op.execute(f"CREATE POLICY upstream_insert ON sip_upstreams FOR INSERT WITH CHECK ({control})")
    op.execute(
        "CREATE POLICY upstream_lock ON sip_upstreams FOR UPDATE USING (true) WITH CHECK (false)"
    )
    op.create_table(
        "sip_egress_permits",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        *(
            sa.Column(name, sa.Uuid(), nullable=False)
            for name in (
                "call_id",
                "account_id",
                "placement_id",
                "upstream_id",
                "asterisk_peer_id",
            )
        ),
        sa.Column(
            "cell_id", sa.Uuid(), sa.ForeignKey("cells.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("placement_generation", sa.BigInteger(), nullable=False),
        sa.Column("upstream_revision", sa.BigInteger(), nullable=False),
        *(
            sa.Column(name, sa.String(64), nullable=False)
            for name in (
                "destination_digest",
                "token_digest",
                "semantic_digest",
            )
        ),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("edge_id", sa.Uuid(), nullable=True),
        sa.Column("boot_id", sa.Uuid(), nullable=True),
        sa.Column("transaction_digest", sa.String(64), nullable=True),
        sa.UniqueConstraint("organization_id", "call_id", name="uq_sip_egress_call"),
        sa.UniqueConstraint("organization_id", "id", name="uq_sip_egress_org_id"),
        sa.UniqueConstraint("token_digest"),
        sa.ForeignKeyConstraint(
            ["organization_id", "call_id"],
            ["telephony_calls.organization_id", "telephony_calls.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "account_id"],
            ["telephony_accounts.organization_id", "telephony_accounts.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["upstream_id", "upstream_revision"],
            ["sip_upstreams.id", "sip_upstreams.revision"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "placement_id"],
            ["organization_placements.organization_id", "organization_placements.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "state IN ('AUTHORIZED','CONSUMED','ENDED','AMBIGUOUS','EXPIRED','REVOKED')",
            name="state_known",
        ),
        sa.CheckConstraint("placement_generation > 0", name="generation_positive"),
        sa.CheckConstraint("expires_at = issued_at + interval '30 seconds'", name="ttl_fixed"),
        sa.CheckConstraint(
            "token_digest ~ '^[a-f0-9]{64}$' AND destination_digest ~ '^[a-f0-9]{64}$' "
            "AND semantic_digest ~ '^[a-f0-9]{64}$'",
            name="digests_valid",
        ),
    )
    op.create_index(
        "ix_sip_egress_permits_organization_id", "sip_egress_permits", ["organization_id"]
    )
    apply_tenant_rls(op, "sip_egress_permits")
    for table in ("sip_egress_routes", "sip_egress_history"):
        columns = [
            sa.Column(
                "organization_id",
                sa.Uuid(),
                sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column(
                "permit_id", sa.Uuid(), primary_key=table == "sip_egress_routes", nullable=False
            ),
            sa.ForeignKeyConstraint(
                ["organization_id", "permit_id"],
                ["sip_egress_permits.organization_id", "sip_egress_permits.id"],
                ondelete="RESTRICT",
            ),
        ]
        if table == "sip_egress_routes":
            columns.extend(
                [
                    sa.Column("peer_id", sa.Uuid(), nullable=False),
                    sa.Column("edge_id", sa.Uuid(), nullable=False),
                    sa.Column("boot_id", sa.Uuid(), nullable=False),
                    sa.Column("transaction_digest", sa.String(64), nullable=False),
                    sa.Column(
                        "authorized_at",
                        sa.DateTime(timezone=True),
                        server_default=sa.func.now(),
                        nullable=False,
                    ),
                    sa.UniqueConstraint(
                        "peer_id", "transaction_digest", name="uq_sip_egress_route_transaction"
                    ),
                ]
            )
        else:
            columns.extend(
                [
                    sa.Column("id", sa.Uuid(), primary_key=True),
                    sa.Column("previous_state", sa.String(16), nullable=True),
                    sa.Column("state", sa.String(16), nullable=False),
                    sa.Column(
                        "occurred_at",
                        sa.DateTime(timezone=True),
                        server_default=sa.func.now(),
                        nullable=False,
                    ),
                ]
            )
        op.create_table(table, *columns)
        op.create_index(f"ix_{table}_organization_id", table, ["organization_id"])
        apply_tenant_rls(op, table)
        op.execute(f"REVOKE UPDATE, DELETE ON {table} FROM nexus_runtime")
    op.create_index("ix_sip_egress_history_permit_id", "sip_egress_history", ["permit_id"])
    op.execute("""
        CREATE FUNCTION nxs_sip_permit_fence() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.state <> 'AUTHORIZED' OR NEW.edge_id IS NOT NULL
                    OR NEW.boot_id IS NOT NULL OR NEW.transaction_digest IS NOT NULL THEN
                    RAISE EXCEPTION 'invalid initial permit' USING ERRCODE = '23514';
                END IF;
                NEW.issued_at := clock_timestamp();
                NEW.expires_at := NEW.issued_at + interval '30 seconds';
            ELSE
                IF (to_jsonb(NEW) - 'state' - 'edge_id' - 'boot_id' - 'transaction_digest')
                    IS DISTINCT FROM
                   (to_jsonb(OLD) - 'state' - 'edge_id' - 'boot_id' - 'transaction_digest') THEN
                    RAISE EXCEPTION 'immutable permit binding' USING ERRCODE = '23514';
                END IF;
                IF NOT (
                    (OLD.state = 'AUTHORIZED' AND NEW.state = 'CONSUMED'
                     AND clock_timestamp() <= OLD.expires_at AND NEW.edge_id IS NOT NULL
                     AND NEW.boot_id IS NOT NULL AND NEW.transaction_digest ~ '^[a-f0-9]{64}$')
                    OR (OLD.state = 'AUTHORIZED' AND NEW.state = 'EXPIRED'
                        AND clock_timestamp() > OLD.expires_at)
                    OR (OLD.state = 'AUTHORIZED' AND NEW.state = 'REVOKED')
                    OR (OLD.state = 'CONSUMED' AND NEW.state IN ('ENDED','AMBIGUOUS'))
                ) OR (NEW.state <> 'CONSUMED' AND
                    (NEW.edge_id,NEW.boot_id,NEW.transaction_digest) IS DISTINCT FROM
                    (OLD.edge_id,OLD.boot_id,OLD.transaction_digest)) THEN
                    RAISE EXCEPTION 'invalid permit transition' USING ERRCODE = '23514';
                END IF;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM public.telephony_calls call
                WHERE call.organization_id = NEW.organization_id AND call.id = NEW.call_id
                AND call.account_id = NEW.account_id AND call.direction = 'OUTBOUND') THEN
                RAISE EXCEPTION 'invalid permit call binding' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER sip_permit_fence BEFORE INSERT OR UPDATE ON sip_egress_permits
        FOR EACH ROW EXECUTE FUNCTION nxs_sip_permit_fence()
    """)
    op.execute("""
        CREATE FUNCTION nxs_sip_permit_history() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            INSERT INTO public.sip_egress_history
                (id, organization_id, permit_id, previous_state, state, occurred_at)
            VALUES (gen_random_uuid(), NEW.organization_id, NEW.id,
                CASE WHEN TG_OP = 'UPDATE' THEN OLD.state ELSE NULL END,
                NEW.state, clock_timestamp());
            IF NEW.state = 'CONSUMED' THEN
                INSERT INTO public.sip_egress_routes
                    (organization_id, permit_id, peer_id, edge_id, boot_id,
                     transaction_digest, authorized_at)
                VALUES (NEW.organization_id, NEW.id, NEW.asterisk_peer_id,
                    NEW.edge_id, NEW.boot_id, NEW.transaction_digest, clock_timestamp());
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER sip_permit_history AFTER INSERT OR UPDATE ON sip_egress_permits
        FOR EACH ROW EXECUTE FUNCTION nxs_sip_permit_history()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER sip_permit_history ON sip_egress_permits")
    op.execute("DROP FUNCTION nxs_sip_permit_history()")
    op.drop_table("sip_egress_history")
    op.drop_table("sip_egress_routes")
    op.execute("DROP TRIGGER sip_permit_fence ON sip_egress_permits")
    op.execute("DROP FUNCTION nxs_sip_permit_fence()")
    op.drop_table("sip_egress_permits")
    op.drop_table("sip_upstreams")
