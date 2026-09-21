"""Durable immutable dialog identity for an already issued inbound route."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from nexus_ai.infrastructure.rls import apply_tenant_rls

revision: str = "d19f0b1c2d3e"
down_revision: str | None = "d19e0b1c2d3e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sip_egress_dialog_bindings",
        sa.Column("permit_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("dialog_digest", sa.String(64), nullable=False),
        sa.Column(
            "established_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "permit_id"],
            ["sip_egress_permits.organization_id", "sip_egress_permits.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("dialog_digest ~ '^[a-f0-9]{64}$'", name="digest_valid"),
    )
    op.create_index(
        "ix_sip_egress_dialog_bindings_organization_id",
        "sip_egress_dialog_bindings",
        ["organization_id"],
    )
    apply_tenant_rls(op, "sip_egress_dialog_bindings")
    op.execute("REVOKE UPDATE, DELETE ON sip_egress_dialog_bindings FROM nexus_runtime")
    op.execute("""
        CREATE FUNCTION nxs_sip_egress_dialog_fence() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            PERFORM 1 FROM public.sip_egress_permits
                WHERE organization_id = NEW.organization_id AND id = NEW.permit_id
                AND state = 'CONSUMED' FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'dialog requires consumed permit' USING ERRCODE = '23514';
            END IF;
            NEW.established_at := clock_timestamp();
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER sip_egress_dialog_fence BEFORE INSERT ON sip_egress_dialog_bindings
        FOR EACH ROW EXECUTE FUNCTION nxs_sip_egress_dialog_fence()
    """)
    op.create_table(
        "sip_dialog_bindings",
        sa.Column("route_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("dialog_digest", sa.String(64), nullable=False),
        sa.Column(
            "established_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "route_id"],
            ["sip_route_authorizations.organization_id", "sip_route_authorizations.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("dialog_digest ~ '^[a-f0-9]{64}$'", name="digest_valid"),
    )
    op.create_index(
        "ix_sip_dialog_bindings_organization_id", "sip_dialog_bindings", ["organization_id"]
    )
    apply_tenant_rls(op, "sip_dialog_bindings")
    op.execute("REVOKE UPDATE, DELETE ON sip_dialog_bindings FROM nexus_runtime")
    op.execute("""
        CREATE FUNCTION nxs_sip_dialog_fence() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            PERFORM 1 FROM public.sip_route_authorizations
                WHERE organization_id = NEW.organization_id AND id = NEW.route_id
                AND state = 'ISSUED' FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'dialog requires issued route' USING ERRCODE = '23514';
            END IF;
            NEW.established_at := clock_timestamp();
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER sip_dialog_fence BEFORE INSERT ON sip_dialog_bindings
        FOR EACH ROW EXECUTE FUNCTION nxs_sip_dialog_fence()
    """)
    op.execute("""
        CREATE FUNCTION nxs_sip_established_fence() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF NEW.state IN ('ESTABLISHED', 'ENDED') AND NOT EXISTS (
                SELECT 1 FROM public.sip_dialog_bindings
                WHERE organization_id = NEW.organization_id AND route_id = NEW.id
            ) THEN
                RAISE EXCEPTION 'missing dialog binding' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER sip_established_fence BEFORE UPDATE ON sip_route_authorizations
        FOR EACH ROW EXECUTE FUNCTION nxs_sip_established_fence()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER sip_egress_dialog_fence ON sip_egress_dialog_bindings")
    op.execute("DROP FUNCTION nxs_sip_egress_dialog_fence()")
    op.drop_table("sip_egress_dialog_bindings")
    op.execute("DROP TRIGGER sip_established_fence ON sip_route_authorizations")
    op.execute("DROP FUNCTION nxs_sip_established_fence()")
    op.execute("DROP TRIGGER sip_dialog_fence ON sip_dialog_bindings")
    op.execute("DROP FUNCTION nxs_sip_dialog_fence()")
    op.drop_table("sip_dialog_bindings")
