"""Fence pre-provider ownership separately from ambiguous provider dispatch."""

import sqlalchemy as sa
from alembic import op

from nexus_ai.infrastructure.rls import apply_tenant_rls

revision = "d19d1b1c2d3e"
down_revision = "d19c1b1c2d3e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sip_call_admissions",
        sa.Column("call_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "call_id"],
            ["telephony_calls.organization_id", "telephony_calls.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("state IN ('PENDING','DISPATCHED','REVOKED')", name="state_known"),
        sa.CheckConstraint("expires_at > created_at", name="deadline_positive"),
    )
    op.create_index(
        "ix_sip_call_admissions_organization_id", "sip_call_admissions", ["organization_id"]
    )
    op.create_index(
        "ix_sip_call_admissions_pending",
        "sip_call_admissions",
        ["organization_id", "state", "expires_at"],
    )
    apply_tenant_rls(op, "sip_call_admissions")
    op.execute("""
        CREATE FUNCTION nxs_sip_admission_fence() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.state <> 'PENDING' THEN
                    RAISE EXCEPTION 'invalid admission origin' USING ERRCODE = '23514';
                END IF;
                NEW.created_at := clock_timestamp();
                NEW.expires_at := NEW.created_at + interval '30 seconds';
            ELSE
                IF (NEW.organization_id, NEW.call_id, NEW.owner_id, NEW.created_at, NEW.expires_at)
                    IS DISTINCT FROM
                    (OLD.organization_id, OLD.call_id, OLD.owner_id, OLD.created_at, OLD.expires_at)
                    OR OLD.state <> 'PENDING' OR NEW.state NOT IN ('DISPATCHED','REVOKED') THEN
                    RAISE EXCEPTION 'admission fence' USING ERRCODE = '23514';
                END IF;
                IF NEW.state = 'DISPATCHED' THEN
                    IF NEW.expires_at <= clock_timestamp() OR NOT EXISTS (
                        SELECT 1 FROM public.sip_egress_permits
                        WHERE organization_id = NEW.organization_id AND call_id = NEW.call_id
                        AND state = 'AUTHORIZED' AND expires_at > clock_timestamp()
                    ) THEN
                        RAISE EXCEPTION 'admission lacks live permit' USING ERRCODE = '23514';
                    END IF;
                END IF;
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute("""
        CREATE TRIGGER sip_admission_fence BEFORE INSERT OR UPDATE ON sip_call_admissions
        FOR EACH ROW EXECUTE FUNCTION nxs_sip_admission_fence();
    """)
    op.execute("""
        CREATE FUNCTION nxs_sip_permit_admission_fence() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        DECLARE admission_state text; admission_deadline timestamptz;
        BEGIN
            SELECT state, expires_at INTO admission_state, admission_deadline
                FROM public.sip_call_admissions
                WHERE organization_id = NEW.organization_id AND call_id = NEW.call_id
                FOR SHARE;
            IF FOUND THEN
                IF (TG_OP = 'INSERT' AND (admission_state <> 'PENDING'
                    OR admission_deadline <= clock_timestamp()))
                    OR (NEW.state = 'CONSUMED' AND admission_state <> 'DISPATCHED') THEN
                    RAISE EXCEPTION 'permit admission fenced' USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute("""
        CREATE TRIGGER sip_permit_admission_fence BEFORE INSERT OR UPDATE ON sip_egress_permits
        FOR EACH ROW EXECUTE FUNCTION nxs_sip_permit_admission_fence();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER sip_permit_admission_fence ON sip_egress_permits")
    op.execute("DROP FUNCTION nxs_sip_permit_admission_fence()")
    op.execute("DROP TRIGGER sip_admission_fence ON sip_call_admissions")
    op.execute("DROP FUNCTION nxs_sip_admission_fence()")
    op.drop_table("sip_call_admissions")
