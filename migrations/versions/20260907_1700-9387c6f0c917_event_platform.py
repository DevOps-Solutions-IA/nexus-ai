"""event platform (NXS-DATA-002, NXS-EVENT-003 / 006 / 007)

Table classification is conscious and documented (ADR-0047 / ADR-0048):

TENANT-OWNED — forced RLS bound to the transaction-local ``nxs.organization_id``:
* event_outbox        — one row per tenant business event; commits atomically with the
                        business mutation, moves to PUBLISHED only after JetStream ack
* event_dead_letters  — a tenant event that failed terminally; sanitized failure info

  Both also get an ``nxs_*_relay`` policy that is visible ONLY to an explicitly unscoped
  system transaction (``nxs.organization_id`` unset). A tenant request always binds a
  scope, so it can never use the relay policy — it stays confined to its own rows by
  ``nxs_tenant_isolation``. The relay policy is what lets the system publisher and the
  dead-letter / replay tooling process every tenant's rows.

PLATFORM-INTERNAL — no RLS, runtime role gets only SELECT / INSERT / UPDATE:
* consumer_receipts   — idempotency bookkeeping (consumer name, globally unique event
                        id, outcome, timestamps); holds no tenant business content, must
                        also cover global events, only ever read by exact key

The runtime role never receives DELETE on any of these tables. Retention is an
administrative job run by the migration role, not an unrestricted runtime delete.

Revision ID: 9387c6f0c917
Revises: 36741ae62327
Create Date: 2026-09-07 17:00:32.990148+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from nexus_ai.infrastructure.rls import (
    DEFAULT_CONTEXT_SETTING,
    DEFAULT_RUNTIME_ROLE,
    apply_tenant_rls,
    drop_tenant_rls,
)

revision: str = "9387c6f0c917"
down_revision: str | None = "36741ae62327"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UNSCOPED = f"nullif(current_setting('{DEFAULT_CONTEXT_SETTING}', true), '') IS NULL"


def _grant_select_insert_update(table: str) -> None:
    op.execute(f'GRANT SELECT, INSERT, UPDATE ON "{table}" TO "{DEFAULT_RUNTIME_ROLE}"')


def _relay_policy(table: str) -> None:
    op.execute(
        f'CREATE POLICY "nxs_system_relay" ON "{table}" '
        f"FOR ALL USING ({_UNSCOPED}) WITH CHECK ({_UNSCOPED})"
    )


def upgrade() -> None:
    # --- PLATFORM-INTERNAL: consumer_receipts ---------------------------------------
    op.create_table(
        "consumer_receipts",
        sa.Column("consumer_name", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=True),
        sa.Column("event_type", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
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
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PROCESSING', 'PROCESSED', 'FAILED', 'DEAD')",
            name=op.f("ck_consumer_receipts_status_known"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 1", name=op.f("ck_consumer_receipts_attempt_count_positive")
        ),
        sa.PrimaryKeyConstraint("consumer_name", "event_id", name=op.f("pk_consumer_receipts")),
    )
    op.create_index(
        "ix_consumer_receipts_event_id", "consumer_receipts", ["event_id"], unique=False
    )
    _grant_select_insert_update("consumer_receipts")

    # --- TENANT-OWNED: event_outbox ------------------------------------------------
    op.create_table(
        "event_outbox",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("event_type", sa.String(length=160), nullable=False),
        sa.Column("event_version", sa.Integer(), nullable=False),
        sa.Column("subject", sa.String(length=240), nullable=False),
        sa.Column("envelope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="PENDING", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("lease_owner", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('PENDING', 'PUBLISHING', 'PUBLISHED', 'FAILED', 'DEAD')",
            name=op.f("ck_event_outbox_status_known"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name=op.f("ck_event_outbox_attempt_count_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_event_outbox_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_outbox")),
    )
    op.create_index(
        "ix_event_outbox_claim", "event_outbox", ["status", "available_at"], unique=False
    )
    op.create_index("ix_event_outbox_lease", "event_outbox", ["lease_expires_at"], unique=False)
    op.create_index(
        "ix_event_outbox_organization_id", "event_outbox", ["organization_id"], unique=False
    )
    apply_tenant_rls(op, "event_outbox")
    _relay_policy("event_outbox")

    # --- TENANT-OWNED: event_dead_letters -----------------------------------------
    op.create_table(
        "event_dead_letters",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("event_type", sa.String(length=160), nullable=False),
        sa.Column("event_version", sa.Integer(), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("consumer_name", sa.String(length=64), nullable=True),
        sa.Column("failure_class", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=False),
        sa.Column("error_summary", sa.String(length=500), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("envelope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("replayed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "origin IN ('OUTBOX_PUBLISH', 'CONSUMER')",
            name=op.f("ck_event_dead_letters_origin_known"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name=op.f("ck_event_dead_letters_attempt_count_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_event_dead_letters_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_dead_letters")),
    )
    op.create_index(
        "ix_event_dead_letters_event_id", "event_dead_letters", ["event_id"], unique=False
    )
    op.create_index(
        "ix_event_dead_letters_organization_id",
        "event_dead_letters",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_event_dead_letters_recorded_at", "event_dead_letters", ["recorded_at"], unique=False
    )
    apply_tenant_rls(op, "event_dead_letters")
    _relay_policy("event_dead_letters")


def downgrade() -> None:
    op.execute('DROP POLICY IF EXISTS "nxs_system_relay" ON "event_dead_letters"')
    drop_tenant_rls(op, "event_dead_letters")
    op.drop_index("ix_event_dead_letters_recorded_at", table_name="event_dead_letters")
    op.drop_index("ix_event_dead_letters_organization_id", table_name="event_dead_letters")
    op.drop_index("ix_event_dead_letters_event_id", table_name="event_dead_letters")
    op.drop_table("event_dead_letters")

    op.execute('DROP POLICY IF EXISTS "nxs_system_relay" ON "event_outbox"')
    drop_tenant_rls(op, "event_outbox")
    op.drop_index("ix_event_outbox_organization_id", table_name="event_outbox")
    op.drop_index("ix_event_outbox_lease", table_name="event_outbox")
    op.drop_index("ix_event_outbox_claim", table_name="event_outbox")
    op.drop_table("event_outbox")

    op.execute(f'REVOKE ALL ON "consumer_receipts" FROM "{DEFAULT_RUNTIME_ROLE}"')
    op.drop_index("ix_consumer_receipts_event_id", table_name="consumer_receipts")
    op.drop_table("consumer_receipts")
