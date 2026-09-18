"""cell placement authority

Revision ID: c18a0b1c2d3e
Revises: f17a0b1c2d3e
Create Date: 2026-09-18 17:13:56.221257+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from nexus_ai.infrastructure.rls import apply_tenant_rls, drop_tenant_rls

revision: str = "c18a0b1c2d3e"
down_revision: str | None = "f17a0b1c2d3e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cells",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("cell_key", sa.String(length=48), nullable=False),
        sa.Column("registration_key_hash", sa.String(length=64), nullable=False),
        sa.Column("registration_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("has_placements", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "NOT (state = 'RETIRED' AND has_placements)", name=op.f("ck_cells_retirement_unbound")
        ),
        sa.CheckConstraint("cell_key ~ '^[a-z][a-z0-9-]{0,47}$'", name=op.f("ck_cells_key_safe")),
        sa.CheckConstraint("state IN ('REGISTERED','RETIRED')", name=op.f("ck_cells_state_known")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cells")),
        sa.UniqueConstraint("cell_key", name=op.f("uq_cells_cell_key")),
        sa.UniqueConstraint("registration_key_hash", name=op.f("uq_cells_registration_key_hash")),
    )
    op.create_table(
        "cell_control_history",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("cell_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.UUID(), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_cell_control_history_actor_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["cell_id"],
            ["cells.id"],
            name=op.f("fk_cell_control_history_cell_id_cells"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cell_control_history")),
        sa.UniqueConstraint(
            "operation", "idempotency_key_hash", name=op.f("uq_cell_control_history_operation")
        ),
        sa.CheckConstraint(
            "operation IN ('REGISTER','RETIRE')",
            name=op.f("ck_cell_control_history_operation_known"),
        ),
    )
    op.create_index(
        op.f("ix_cell_control_history_cell_id"), "cell_control_history", ["cell_id"], unique=False
    )
    op.create_table(
        "organization_placements",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("cell_id", sa.Uuid(), nullable=False),
        sa.Column("assignment_generation", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "state IN ('ACTIVE','SUSPENDED')", name=op.f("ck_organization_placements_state_known")
        ),
        sa.CheckConstraint(
            "assignment_generation >= 1",
            name=op.f("ck_organization_placements_generation_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["cell_id"],
            ["cells.id"],
            name=op.f("fk_organization_placements_cell_id_cells"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_organization_placements_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization_placements")),
        sa.UniqueConstraint("organization_id", "id", name="uq_organization_placements_org_id"),
        sa.UniqueConstraint(
            "organization_id", name=op.f("uq_organization_placements_organization_id")
        ),
        info={"tenant_scoped": "owned"},
    )
    op.create_index(
        op.f("ix_organization_placements_cell_id"),
        "organization_placements",
        ["cell_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_organization_placements_organization_id"),
        "organization_placements",
        ["organization_id"],
        unique=False,
    )
    op.create_table(
        "placement_mutations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("placement_id", sa.Uuid(), nullable=False),
        sa.Column("cell_id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("expected_generation", sa.BigInteger(), nullable=True),
        sa.Column("result_generation", sa.BigInteger(), nullable=False),
        sa.Column("previous_state", sa.String(length=16), nullable=True),
        sa.Column("new_state", sa.String(length=16), nullable=False),
        sa.Column("actor_user_id", sa.UUID(), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "new_state IN ('ACTIVE','SUSPENDED')",
            name=op.f("ck_placement_mutations_new_state_known"),
        ),
        sa.CheckConstraint(
            "operation IN ('ASSIGN','SUSPEND','RESUME')",
            name=op.f("ck_placement_mutations_operation_known"),
        ),
        sa.CheckConstraint(
            "previous_state IS NULL OR previous_state IN ('ACTIVE','SUSPENDED')",
            name=op.f("ck_placement_mutations_previous_state_known"),
        ),
        sa.CheckConstraint(
            "expected_generation IS NULL OR expected_generation >= 1",
            name=op.f("ck_placement_mutations_expected_generation_positive"),
        ),
        sa.CheckConstraint(
            "result_generation >= 1", name=op.f("ck_placement_mutations_result_generation_positive")
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_placement_mutations_actor_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["cell_id"],
            ["cells.id"],
            name=op.f("fk_placement_mutations_cell_id_cells"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "placement_id"],
            ["organization_placements.organization_id", "organization_placements.id"],
            name=op.f("fk_placement_mutations_organization_id_organization_placements"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_placement_mutations_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_placement_mutations")),
        sa.UniqueConstraint(
            "organization_id",
            "operation",
            "idempotency_key_hash",
            name=op.f("uq_placement_mutations_organization_id"),
        ),
        info={"tenant_scoped": "owned"},
    )
    op.create_index(
        op.f("ix_placement_mutations_organization_id"),
        "placement_mutations",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_placement_mutations_placement_id"),
        "placement_mutations",
        ["placement_id"],
        unique=False,
    )
    op.drop_constraint(
        op.f("ck_platform_grants_capability_known"), "platform_grants", type_="check"
    )
    op.create_check_constraint(
        "capability_known",
        "platform_grants",
        "capability IN ('organization:create','cell:control')",
    )
    control = (
        "EXISTS (SELECT 1 FROM platform_grants grant_row JOIN users actor "
        "ON actor.id = grant_row.user_id WHERE grant_row.user_id = "
        "nullif(current_setting('nxs.principal_id', true), '')::uuid "
        "AND grant_row.capability = 'cell:control' AND actor.status = 'ACTIVE')"
    )
    for table in ("cells", "cell_control_history"):
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        read_policy = "true" if table == "cells" else control
        op.execute(f'CREATE POLICY cell_read ON "{table}" FOR SELECT USING ({read_policy})')
        op.execute(f'CREATE POLICY cell_insert ON "{table}" FOR INSERT WITH CHECK ({control})')
        op.execute(
            f'CREATE POLICY cell_update ON "{table}" FOR UPDATE USING (true) WITH CHECK ({control})'
        )
        op.execute(f'GRANT SELECT, INSERT, UPDATE ON "{table}" TO nexus_runtime')
    for table in ("organization_placements", "placement_mutations"):
        apply_tenant_rls(op, table)
        op.execute(
            f'CREATE POLICY cell_control_insert ON "{table}" AS RESTRICTIVE '
            f"FOR INSERT WITH CHECK ({control})"
        )
        op.execute(
            f'CREATE POLICY cell_control_update ON "{table}" AS RESTRICTIVE '
            f"FOR UPDATE USING (true) WITH CHECK ({control})"
        )
    op.execute("""
        CREATE FUNCTION nxs_cell_catalog_fence() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF NEW.id <> OLD.id OR NEW.cell_key <> OLD.cell_key
                OR NEW.registration_key_hash <> OLD.registration_key_hash
                OR NEW.registration_fingerprint <> OLD.registration_fingerprint
                OR (OLD.has_placements AND NOT NEW.has_placements)
                OR (OLD.state = 'RETIRED' AND NEW.state <> OLD.state) THEN
                RAISE EXCEPTION 'immutable Cell identity or terminal state' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER cell_catalog_fence BEFORE UPDATE ON cells "
        "FOR EACH ROW EXECUTE FUNCTION nxs_cell_catalog_fence()"
    )
    op.execute("""
        CREATE FUNCTION nxs_placement_fence() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.state <> 'ACTIVE' OR NEW.assignment_generation <> 1 THEN
                    RAISE EXCEPTION 'invalid initial placement' USING ERRCODE = '23514';
                END IF;
                UPDATE public.cells SET has_placements = true
                    WHERE id = NEW.cell_id AND state = 'REGISTERED';
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'unavailable Cell' USING ERRCODE = '23514';
                END IF;
            ELSE
                IF NEW.id <> OLD.id OR NEW.organization_id <> OLD.organization_id
                    OR NEW.cell_id <> OLD.cell_id
                    OR NEW.state = OLD.state
                    OR NEW.assignment_generation <> OLD.assignment_generation + 1 THEN
                    RAISE EXCEPTION 'invalid placement transition' USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER placement_fence BEFORE INSERT OR UPDATE ON organization_placements "
        "FOR EACH ROW EXECUTE FUNCTION nxs_placement_fence()"
    )
    op.execute("""
        CREATE FUNCTION nxs_cell_history_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'immutable control history' USING ERRCODE = '23514';
        END $$
    """)
    for table in ("cell_control_history", "placement_mutations"):
        op.execute(
            f'CREATE TRIGGER cell_history_immutable BEFORE UPDATE OR DELETE ON "{table}" '
            "FOR EACH ROW EXECUTE FUNCTION nxs_cell_history_immutable()"
        )


def downgrade() -> None:
    for table in ("organization_placements", "placement_mutations"):
        drop_tenant_rls(op, table)
    op.execute("DROP TRIGGER placement_fence ON organization_placements")
    op.execute("DROP FUNCTION nxs_placement_fence()")
    op.execute("DROP TRIGGER cell_catalog_fence ON cells")
    op.execute("DROP FUNCTION nxs_cell_catalog_fence()")
    for table in ("cell_control_history", "placement_mutations"):
        op.execute(f'DROP TRIGGER cell_history_immutable ON "{table}"')
    op.execute("DROP FUNCTION nxs_cell_history_immutable()")
    op.execute("DELETE FROM platform_grants WHERE capability = 'cell:control'")
    op.drop_constraint(
        op.f("ck_platform_grants_capability_known"), "platform_grants", type_="check"
    )
    op.create_check_constraint(
        "capability_known", "platform_grants", "capability IN ('organization:create')"
    )
    op.drop_index(op.f("ix_placement_mutations_placement_id"), table_name="placement_mutations")
    op.drop_index(op.f("ix_placement_mutations_organization_id"), table_name="placement_mutations")
    op.drop_table("placement_mutations")
    op.drop_index(
        op.f("ix_organization_placements_organization_id"), table_name="organization_placements"
    )
    op.drop_index(op.f("ix_organization_placements_cell_id"), table_name="organization_placements")
    op.drop_table("organization_placements")
    op.drop_index(op.f("ix_cell_control_history_cell_id"), table_name="cell_control_history")
    op.drop_table("cell_control_history")
    op.drop_table("cells")
