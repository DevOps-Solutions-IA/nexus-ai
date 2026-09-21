"""Platform peer policy has a durable revision and append-only control history."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "d19a1b1c2d3e"
down_revision: str | None = "d19f0b1c2d3e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sip_peer_profiles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("profile", JSONB(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.CheckConstraint("revision > 0", name="revision_positive"),
        sa.CheckConstraint("octet_length(profile::text) <= 16384", name="profile_bounded"),
    )
    op.create_table(
        "sip_peer_history",
        sa.Column(
            "peer_id",
            sa.Uuid(),
            sa.ForeignKey("sip_peer_profiles.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("revision", sa.BigInteger(), primary_key=True),
        sa.Column("profile", JSONB(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    control = (
        "EXISTS (SELECT 1 FROM platform_grants grant_row JOIN users actor "
        "ON actor.id = grant_row.user_id WHERE grant_row.user_id = "
        "nullif(current_setting('nxs.principal_id', true), '')::uuid "
        "AND grant_row.capability = 'sip_edge:control' AND actor.status = 'ACTIVE')"
    )
    for table in ("sip_peer_profiles", "sip_peer_history"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON {table} FROM nexus_runtime")
        op.execute(f"GRANT SELECT, INSERT ON {table} TO nexus_runtime")
        op.execute(f"CREATE POLICY peer_read ON {table} FOR SELECT USING (true)")
        op.execute(f"CREATE POLICY peer_insert ON {table} FOR INSERT WITH CHECK ({control})")
    op.execute("GRANT UPDATE ON sip_peer_profiles TO nexus_runtime")
    op.execute(
        "CREATE POLICY peer_update ON sip_peer_profiles FOR UPDATE USING (true) "
        f"WITH CHECK ({control})"
    )
    op.execute("""
        CREATE FUNCTION nxs_sip_peer_fence() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF (TG_OP = 'INSERT' AND NEW.revision <> 1)
                OR (TG_OP = 'UPDATE' AND (NEW.id <> OLD.id OR NEW.revision <> OLD.revision + 1))
                OR NEW.profile->>'peer_id' IS DISTINCT FROM NEW.id::text THEN
                RAISE EXCEPTION 'invalid peer revision' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER sip_peer_fence BEFORE INSERT OR UPDATE ON sip_peer_profiles
        FOR EACH ROW EXECUTE FUNCTION nxs_sip_peer_fence()
    """)
    op.execute("""
        CREATE FUNCTION nxs_sip_peer_history() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            INSERT INTO public.sip_peer_history(peer_id, revision, profile, active, occurred_at)
                VALUES (NEW.id, NEW.revision, NEW.profile, NEW.active, clock_timestamp());
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER sip_peer_history AFTER INSERT OR UPDATE ON sip_peer_profiles
        FOR EACH ROW EXECUTE FUNCTION nxs_sip_peer_history()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER sip_peer_history ON sip_peer_profiles")
    op.execute("DROP FUNCTION nxs_sip_peer_history()")
    op.execute("DROP TRIGGER sip_peer_fence ON sip_peer_profiles")
    op.execute("DROP FUNCTION nxs_sip_peer_fence()")
    op.drop_table("sip_peer_history")
    op.drop_table("sip_peer_profiles")
