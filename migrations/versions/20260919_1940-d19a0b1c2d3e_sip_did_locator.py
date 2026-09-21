"""Transactional SIP DID discovery projection, not ownership authority."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from nexus_ai.infrastructure.rls import apply_tenant_rls, drop_tenant_rls

revision: str = "d19a0b1c2d3e"
down_revision: str | None = "c18a0b1c2d3e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_telephony_number_locator_source",
        "telephony_phone_numbers",
        ["organization_id", "id", "account_id", "e164"],
    )
    op.create_table(
        "sip_did_locators",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("phone_number_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("e164", sa.String(16), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("e164"),
        sa.UniqueConstraint("phone_number_id"),
        sa.UniqueConstraint("organization_id", "id", name="uq_sip_did_locators_org_id"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["organization_id", "phone_number_id", "account_id", "e164"],
            [
                "telephony_phone_numbers.organization_id",
                "telephony_phone_numbers.id",
                "telephony_phone_numbers.account_id",
                "telephony_phone_numbers.e164",
            ],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        sa.CheckConstraint("revision > 0", name="revision_positive"),
        sa.CheckConstraint(r"e164 ~ '^\+[1-9][0-9]{6,14}$'", name="e164_form"),
    )
    op.create_index("ix_sip_did_locators_organization_id", "sip_did_locators", ["organization_id"])
    op.create_index("ix_sip_did_locators_account_id", "sip_did_locators", ["account_id"])
    apply_tenant_rls(op, "sip_did_locators", allow_delete=True)
    op.execute("GRANT SELECT ON sip_did_locators TO nexus_sip_locator")
    op.execute("""
        CREATE POLICY sip_locator_discovery ON sip_did_locators
        AS RESTRICTIVE
        FOR SELECT TO nexus_sip_locator
        USING (e164 = nullif(current_setting('nxs.sip_lookup_e164', true), '') AND active)
    """)
    op.execute(
        "CREATE POLICY sip_locator_reader ON sip_did_locators "
        "FOR SELECT TO nexus_sip_locator USING (true)"
    )
    op.execute("""
        CREATE FUNCTION nxs_sip_locator_fence() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF TG_OP = 'UPDATE' AND (NEW.id <> OLD.id
                OR NEW.organization_id <> OLD.organization_id
                OR NEW.phone_number_id <> OLD.phone_number_id
                OR NEW.revision <> OLD.revision + 1) THEN
                RAISE EXCEPTION 'invalid locator transition' USING ERRCODE = '23514';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.telephony_phone_numbers number
                JOIN public.telephony_accounts account ON account.id = number.account_id
                    AND account.organization_id = number.organization_id
                WHERE number.id = NEW.phone_number_id
                    AND number.organization_id = NEW.organization_id
                    AND number.account_id = NEW.account_id AND number.e164 = NEW.e164
                    AND NEW.active = (number.verified AND number.inbound_enabled
                                      AND account.status = 'ACTIVE')
            ) THEN
                RAISE EXCEPTION 'invalid locator source' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER sip_locator_fence BEFORE INSERT OR UPDATE ON sip_did_locators
        FOR EACH ROW EXECUTE FUNCTION nxs_sip_locator_fence()
    """)
    op.execute("""
        CREATE FUNCTION nxs_sip_number_projection() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        DECLARE account_active boolean;
        BEGIN
            SELECT status = 'ACTIVE' INTO STRICT account_active
                FROM public.telephony_accounts
                WHERE organization_id = NEW.organization_id AND id = NEW.account_id
                FOR SHARE;
            INSERT INTO public.sip_did_locators
                (id, organization_id, phone_number_id, account_id, e164, revision, active)
            VALUES (gen_random_uuid(), NEW.organization_id, NEW.id, NEW.account_id,
                    NEW.e164, 1, NEW.verified AND NEW.inbound_enabled AND account_active)
            ON CONFLICT (phone_number_id) DO UPDATE
                SET active = EXCLUDED.active, revision = sip_did_locators.revision + 1,
                    updated_at = clock_timestamp();
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER sip_number_projection AFTER INSERT OR UPDATE OF verified, inbound_enabled
        ON telephony_phone_numbers FOR EACH ROW EXECUTE FUNCTION nxs_sip_number_projection()
    """)
    op.execute("""
        CREATE FUNCTION nxs_sip_account_projection() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            PERFORM id FROM public.telephony_phone_numbers
                WHERE organization_id = NEW.organization_id AND account_id = NEW.id
                ORDER BY id FOR UPDATE;
            PERFORM id FROM public.sip_did_locators
                WHERE organization_id = NEW.organization_id AND account_id = NEW.id
                ORDER BY e164 FOR UPDATE;
            UPDATE public.sip_did_locators locator
                SET active = number.verified AND number.inbound_enabled AND NEW.status = 'ACTIVE',
                    revision = locator.revision + 1, updated_at = clock_timestamp()
                FROM public.telephony_phone_numbers number
                WHERE locator.organization_id = NEW.organization_id
                    AND locator.account_id = NEW.id AND number.id = locator.phone_number_id
                    AND number.organization_id = NEW.organization_id;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER sip_account_projection AFTER UPDATE OF status ON telephony_accounts
        FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status)
        EXECUTE FUNCTION nxs_sip_account_projection()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER sip_account_projection ON telephony_accounts")
    op.execute("DROP FUNCTION nxs_sip_account_projection()")
    op.execute("DROP TRIGGER sip_number_projection ON telephony_phone_numbers")
    op.execute("DROP FUNCTION nxs_sip_number_projection()")
    op.execute("DROP TRIGGER sip_locator_fence ON sip_did_locators")
    op.execute("DROP FUNCTION nxs_sip_locator_fence()")
    drop_tenant_rls(op, "sip_did_locators")
    op.drop_table("sip_did_locators")
    op.drop_constraint(
        "uq_telephony_number_locator_source", "telephony_phone_numbers", type_="unique"
    )
