"""Tenant-confined inbound authorizations and global target drainage references."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from nexus_ai.infrastructure.rls import apply_tenant_rls

revision: str = "d19d0b1c2d3e"
down_revision: str | None = "d19c0b1c2d3e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("cell_sip_targets", "cell_sip_target_heads"):
        op.execute(f'ALTER POLICY sip_control_update ON "{table}" USING (true)')
    op.create_table(
        "sip_route_authorizations",
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
                "peer_id",
                "phone_number_id",
                "account_id",
                "placement_id",
                "cell_id",
                "target_id",
                "edge_id",
                "boot_id",
            )
        ),
        sa.Column("e164", sa.String(16), nullable=False),
        sa.Column("placement_generation", sa.BigInteger(), nullable=False),
        sa.Column("target_revision", sa.BigInteger(), nullable=False),
        sa.Column("transaction_digest", sa.String(64), nullable=False),
        sa.Column("semantic_digest", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("issue_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("organization_id", "id", name="uq_sip_routes_org_id"),
        sa.UniqueConstraint("peer_id", "transaction_digest", name="uq_sip_routes_transaction"),
        sa.ForeignKeyConstraint(
            ["organization_id", "phone_number_id", "account_id", "e164"],
            [
                "telephony_phone_numbers.organization_id",
                "telephony_phone_numbers.id",
                "telephony_phone_numbers.account_id",
                "telephony_phone_numbers.e164",
            ],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "placement_id"],
            ["organization_placements.organization_id", "organization_placements.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["cell_id", "target_id"],
            ["cell_sip_targets.cell_id", "cell_sip_targets.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "placement_generation > 0 AND target_revision > 0", name="positive_fences"
        ),
        sa.CheckConstraint(
            "state IN ('AUTHORIZED','ISSUED','ESTABLISHED','ENDED','FAILED','AMBIGUOUS','EXPIRED')",
            name="state_known",
        ),
        sa.CheckConstraint(
            "issue_deadline > authorized_at "
            "AND issue_deadline <= authorized_at + interval '5 seconds'",
            name="issue_window",
        ),
        sa.CheckConstraint(
            "transaction_digest ~ '^[a-f0-9]{64}$' AND semantic_digest ~ '^[a-f0-9]{64}$'",
            name="digests_valid",
        ),
    )
    op.create_index(
        "ix_sip_route_authorizations_organization_id",
        "sip_route_authorizations",
        ["organization_id"],
    )
    apply_tenant_rls(op, "sip_route_authorizations")
    op.create_table(
        "sip_route_history",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("route_id", sa.Uuid(), nullable=False),
        sa.Column("previous_state", sa.String(16), nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("edge_id", sa.Uuid(), nullable=False),
        sa.Column("boot_id", sa.Uuid(), nullable=False),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "route_id"],
            ["sip_route_authorizations.organization_id", "sip_route_authorizations.id"],
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_sip_route_history_organization_id", "sip_route_history", ["organization_id"]
    )
    op.create_index("ix_sip_route_history_route_id", "sip_route_history", ["route_id"])
    apply_tenant_rls(op, "sip_route_history")
    op.execute("REVOKE UPDATE, DELETE ON sip_route_history FROM nexus_runtime")
    op.create_table(
        "sip_target_route_references",
        sa.Column(
            "route_id",
            sa.Uuid(),
            sa.ForeignKey("sip_route_authorizations.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "target_id",
            sa.Uuid(),
            sa.ForeignKey("cell_sip_targets.id", ondelete="RESTRICT"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_sip_target_route_references_target_id", "sip_target_route_references", ["target_id"]
    )
    op.execute("ALTER TABLE sip_target_route_references ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE sip_target_route_references FORCE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON sip_target_route_references FROM nexus_runtime")
    op.execute("GRANT SELECT, INSERT, DELETE ON sip_target_route_references TO nexus_runtime")
    op.execute(
        "CREATE POLICY reference_read ON sip_target_route_references FOR SELECT USING (true)"
    )
    op.execute("""
        CREATE POLICY reference_insert ON sip_target_route_references FOR INSERT
        WITH CHECK (EXISTS (SELECT 1 FROM sip_route_authorizations route
            WHERE route.id = route_id AND route.target_id = sip_target_route_references.target_id
            AND route.state IN ('AUTHORIZED','ISSUED','ESTABLISHED','AMBIGUOUS')))
    """)
    op.execute("""
        CREATE POLICY reference_delete ON sip_target_route_references FOR DELETE
        USING (EXISTS (SELECT 1 FROM sip_route_authorizations route
            WHERE route.id = route_id AND route.target_id = sip_target_route_references.target_id
            AND route.state IN ('ENDED','FAILED','EXPIRED')))
    """)
    op.execute("""
        CREATE FUNCTION nxs_sip_route_fence() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.state <> 'AUTHORIZED' OR NEW.issued_at IS NOT NULL THEN
                    RAISE EXCEPTION 'invalid initial route' USING ERRCODE = '23514';
                END IF;
                NEW.authorized_at := clock_timestamp();
                NEW.issue_deadline := NEW.authorized_at + interval '5 seconds';
                PERFORM 1 FROM public.cell_sip_targets
                    WHERE id = NEW.target_id AND cell_id = NEW.cell_id
                      AND target_revision = NEW.target_revision AND state = 'ACTIVE'
                    FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'invalid route target' USING ERRCODE = '23514';
                END IF;
            ELSE
                IF (to_jsonb(NEW) - 'state' - 'issued_at') IS DISTINCT FROM
                   (to_jsonb(OLD) - 'state' - 'issued_at') THEN
                    RAISE EXCEPTION 'immutable route binding' USING ERRCODE = '23514';
                END IF;
                IF NOT (
                    (OLD.state = 'AUTHORIZED' AND NEW.state = 'ISSUED'
                     AND clock_timestamp() <= OLD.issue_deadline AND NEW.issued_at IS NOT NULL)
                    OR (OLD.state = 'AUTHORIZED' AND NEW.state = 'EXPIRED'
                        AND clock_timestamp() > OLD.issue_deadline)
                    OR (OLD.state = 'ISSUED' AND NEW.state IN ('ESTABLISHED','FAILED','AMBIGUOUS'))
                    OR (OLD.state = 'ESTABLISHED' AND NEW.state IN ('ENDED','AMBIGUOUS'))
                ) OR (OLD.state <> 'AUTHORIZED' AND NEW.issued_at IS DISTINCT FROM OLD.issued_at)
                  OR (NEW.state = 'EXPIRED' AND NEW.issued_at IS NOT NULL) THEN
                    RAISE EXCEPTION 'invalid route transition' USING ERRCODE = '23514';
                END IF;
                IF NEW.state = 'ISSUED' THEN
                    NEW.issued_at := clock_timestamp();
                END IF;
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER sip_route_fence BEFORE INSERT OR UPDATE ON sip_route_authorizations
        FOR EACH ROW EXECUTE FUNCTION nxs_sip_route_fence()
    """)
    op.execute("""
        CREATE FUNCTION nxs_sip_route_reference() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                INSERT INTO public.sip_target_route_references (route_id,target_id)
                    VALUES (NEW.id,NEW.target_id);
            ELSIF NEW.state IN ('ENDED','FAILED','EXPIRED') THEN
                DELETE FROM public.sip_target_route_references WHERE route_id = NEW.id;
            END IF;
            INSERT INTO public.sip_route_history
                (id,organization_id,route_id,previous_state,state,edge_id,boot_id,occurred_at)
                VALUES (gen_random_uuid(),NEW.organization_id,NEW.id,
                        CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE OLD.state END,
                        NEW.state,NEW.edge_id,NEW.boot_id,clock_timestamp());
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER sip_route_reference AFTER INSERT OR UPDATE ON sip_route_authorizations
        FOR EACH ROW EXECUTE FUNCTION nxs_sip_route_reference()
    """)
    op.execute("""
        CREATE FUNCTION nxs_sip_retirement_fence() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF NEW.state = 'RETIRED' THEN
                PERFORM 1 FROM public.cell_sip_targets WHERE id = OLD.id FOR UPDATE;
                IF EXISTS (SELECT 1 FROM public.sip_target_route_references
                           WHERE target_id = OLD.id) THEN
                    RAISE EXCEPTION 'target has outstanding routes' USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER sip_retirement_fence BEFORE UPDATE ON cell_sip_targets
        FOR EACH ROW EXECUTE FUNCTION nxs_sip_retirement_fence()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER sip_retirement_fence ON cell_sip_targets")
    op.execute("DROP FUNCTION nxs_sip_retirement_fence()")
    op.execute("DROP TRIGGER sip_route_reference ON sip_route_authorizations")
    op.execute("DROP FUNCTION nxs_sip_route_reference()")
    op.execute("DROP TRIGGER sip_route_fence ON sip_route_authorizations")
    op.execute("DROP FUNCTION nxs_sip_route_fence()")
    op.drop_table("sip_target_route_references")
    op.drop_table("sip_route_history")
    op.drop_table("sip_route_authorizations")
    control = (
        "EXISTS (SELECT 1 FROM platform_grants grant_row JOIN users actor "
        "ON actor.id = grant_row.user_id WHERE grant_row.user_id = "
        "nullif(current_setting('nxs.principal_id', true), '')::uuid "
        "AND grant_row.capability = 'sip_edge:control' AND actor.status = 'ACTIVE')"
    )
    for table in ("cell_sip_targets", "cell_sip_target_heads"):
        op.execute(f'ALTER POLICY sip_control_update ON "{table}" USING ({control})')
