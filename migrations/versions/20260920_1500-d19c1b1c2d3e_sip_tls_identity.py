"""Bind immutable target and upstream revisions to exact TLS identities."""

import sqlalchemy as sa
from alembic import op

revision = "d19c1b1c2d3e"
down_revision = "d19b1b1c2d3e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("cell_sip_targets", "sip_upstreams"):
        op.add_column(table, sa.Column("certificate_sha256", sa.String(64), nullable=True))
        op.create_check_constraint(
            "tls_identity",
            table,
            "(transport = 'TLS' AND certificate_sha256 IS NOT NULL "
            "AND certificate_sha256 ~ '^[a-f0-9]{64}$') OR "
            "(transport <> 'TLS' AND certificate_sha256 IS NULL)",
        )
    op.execute(
        """
        CREATE FUNCTION nxs_sip_tls_identity_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.certificate_sha256 IS DISTINCT FROM OLD.certificate_sha256 THEN
                RAISE EXCEPTION 'immutable TLS identity' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER sip_target_tls_identity_immutable
            BEFORE UPDATE ON cell_sip_targets FOR EACH ROW
            EXECUTE FUNCTION nxs_sip_tls_identity_immutable();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER sip_target_tls_identity_immutable ON cell_sip_targets")
    op.execute("DROP FUNCTION nxs_sip_tls_identity_immutable()")
    for table in ("sip_upstreams", "cell_sip_targets"):
        op.drop_constraint(op.f(f"ck_{table}_tls_identity"), table, type_="check")
        op.drop_column(table, "certificate_sha256")
