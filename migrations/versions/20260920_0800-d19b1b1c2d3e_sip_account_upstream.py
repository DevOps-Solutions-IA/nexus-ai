"""Tenant-bound operations-controlled upstream selection, not request authority."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from nexus_ai.infrastructure.rls import apply_tenant_rls

revision: str = "d19b1b1c2d3e"
down_revision: str | None = "d19a1b1c2d3e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sip_account_upstreams",
        sa.Column("account_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("upstream_id", sa.Uuid(), nullable=False),
        sa.Column("upstream_revision", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
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
        sa.CheckConstraint("revision > 0", name="revision_positive"),
    )
    op.create_index(
        "ix_sip_account_upstreams_organization_id", "sip_account_upstreams", ["organization_id"]
    )
    apply_tenant_rls(op, "sip_account_upstreams")
    op.create_table(
        "sip_account_upstream_history",
        sa.Column("account_id", sa.Uuid(), primary_key=True),
        sa.Column("revision", sa.BigInteger(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("upstream_id", sa.Uuid(), nullable=False),
        sa.Column("upstream_revision", sa.BigInteger(), nullable=False),
        sa.Column(
            "actor_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
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
    )
    op.create_index(
        "ix_sip_account_upstream_history_organization_id",
        "sip_account_upstream_history",
        ["organization_id"],
    )
    apply_tenant_rls(op, "sip_account_upstream_history")
    control = (
        "EXISTS (SELECT 1 FROM platform_grants grant_row JOIN users actor "
        "ON actor.id = grant_row.user_id WHERE grant_row.user_id = "
        "nullif(current_setting('nxs.principal_id', true), '')::uuid "
        "AND grant_row.capability = 'sip_edge:control' AND actor.status = 'ACTIVE')"
    )
    op.execute(
        "CREATE POLICY upstream_binding_insert ON sip_account_upstreams "
        f"AS RESTRICTIVE FOR INSERT WITH CHECK ({control})"
    )
    op.execute(
        "CREATE POLICY upstream_binding_update ON sip_account_upstreams "
        f"AS RESTRICTIVE FOR UPDATE USING (true) WITH CHECK ({control})"
    )
    op.execute(
        "CREATE POLICY upstream_binding_delete ON sip_account_upstreams "
        "AS RESTRICTIVE FOR DELETE USING (false)"
    )
    op.execute(
        "CREATE POLICY upstream_history_insert ON sip_account_upstream_history "
        f"AS RESTRICTIVE FOR INSERT WITH CHECK (({control}) AND pg_trigger_depth() > 0)"
    )
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE POLICY upstream_history_{operation.lower()} "
            f"ON sip_account_upstream_history AS RESTRICTIVE FOR {operation} USING (false)"
        )
    op.execute("""
        CREATE FUNCTION nxs_sip_account_upstream_fence() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF (TG_OP = 'INSERT' AND NEW.revision <> 1)
                OR (TG_OP = 'UPDATE' AND (NEW.organization_id <> OLD.organization_id
                OR NEW.account_id <> OLD.account_id OR NEW.revision <> OLD.revision + 1)) THEN
                RAISE EXCEPTION 'invalid account upstream revision' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER sip_account_upstream_fence BEFORE INSERT OR UPDATE ON sip_account_upstreams
        FOR EACH ROW EXECUTE FUNCTION nxs_sip_account_upstream_fence()
    """)
    op.execute("""
        CREATE FUNCTION nxs_sip_account_upstream_history() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            INSERT INTO sip_account_upstream_history
                (organization_id, account_id, revision, upstream_id, upstream_revision,
                 actor_user_id, occurred_at)
            VALUES (NEW.organization_id, NEW.account_id, NEW.revision, NEW.upstream_id,
                    NEW.upstream_revision,
                    nullif(current_setting('nxs.principal_id', true), '')::uuid,
                    clock_timestamp());
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER sip_account_upstream_history AFTER INSERT OR UPDATE ON sip_account_upstreams
        FOR EACH ROW EXECUTE FUNCTION nxs_sip_account_upstream_history()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER sip_account_upstream_history ON sip_account_upstreams")
    op.execute("DROP FUNCTION nxs_sip_account_upstream_history()")
    op.drop_table("sip_account_upstream_history")
    op.execute("DROP TRIGGER sip_account_upstream_fence ON sip_account_upstreams")
    op.execute("DROP FUNCTION nxs_sip_account_upstream_fence()")
    op.drop_table("sip_account_upstreams")
