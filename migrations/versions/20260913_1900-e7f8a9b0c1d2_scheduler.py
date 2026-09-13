"""durable tenant-isolated Scheduler (NXS-P15: NXS-SCHED-001)

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-09-13 19:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from nexus_ai.domain.auth.rbac import PERMISSION_IDS, ROLE_IDS, PermissionKey, RoleKey
from nexus_ai.infrastructure.rls import apply_tenant_rls, drop_tenant_rls

revision: str = "e7f8a9b0c1d2"
down_revision: str | None = "d6e7f8a9b0c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSONB = postgresql.JSONB(astext_type=sa.Text())
_NOW = sa.text("now()")
_PERMISSIONS = (
    (PermissionKey.SCHEDULE_READ, "Read schedules, occurrences and transition history"),
    (PermissionKey.SCHEDULE_EXECUTE, "Activate, pause, resume and cancel schedules"),
    (PermissionKey.SCHEDULE_CONFIGURE, "Create and edit schedule definitions"),
)


def _org_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id"],
        ["organizations.id"],
        name=op.f(f"fk_{table}_organization_id_organizations"),
        ondelete="RESTRICT",
    )


def upgrade() -> None:
    op.create_table(
        "scheduler_schedules",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("schedule_key", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("workflow_version_id", sa.UUID(), nullable=False),
        sa.Column("schedule_type", sa.String(16), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("recurrence_spec", _JSONB, nullable=True),
        sa.Column("input_payload", _JSONB, nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_local_time", sa.DateTime(timezone=False), nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("misfire_policy", sa.String(24), nullable=False),
        sa.Column("max_catch_up", sa.Integer(), server_default="1", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("timezone_data_version", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.CheckConstraint("target_type = 'START_WORKFLOW'", name="ck_scheduler_schedule_target"),
        sa.CheckConstraint(
            "schedule_type IN ('ONE_TIME','RECURRING')", name="ck_scheduler_schedule_type"
        ),
        sa.CheckConstraint(
            "state IN ('DRAFT','ACTIVE','PAUSED','COMPLETED','CANCELLED')",
            name="ck_scheduler_schedule_state",
        ),
        sa.CheckConstraint(
            "misfire_policy IN ('SKIP','FIRE_ONCE','CATCH_UP_BOUNDED')",
            name="ck_scheduler_schedule_misfire",
        ),
        sa.CheckConstraint("max_catch_up BETWEEN 1 AND 100", name="ck_scheduler_schedule_catch_up"),
        sa.CheckConstraint("revision >= 1", name="ck_scheduler_schedule_revision"),
        sa.CheckConstraint(
            "end_at IS NULL OR end_at >= start_at", name="ck_scheduler_schedule_window"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workflow_version_id"],
            ["workflow_versions.organization_id", "workflow_versions.id"],
            name="fk_scheduler_schedules_org_workflow_version",
            ondelete="RESTRICT",
        ),
        _org_fk("scheduler_schedules"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scheduler_schedules")),
        sa.UniqueConstraint("organization_id", "id", name="uq_scheduler_schedules_org_id"),
        sa.UniqueConstraint(
            "organization_id", "schedule_key", name="uq_scheduler_schedules_org_key"
        ),
    )
    op.create_index(
        "ix_scheduler_schedules_due",
        "scheduler_schedules",
        ["organization_id", "state", "next_fire_at"],
    )
    op.create_index(
        "ix_scheduler_schedules_organization_id", "scheduler_schedules", ["organization_id"]
    )
    apply_tenant_rls(op, "scheduler_schedules")

    op.create_table(
        "scheduler_occurrences",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("schedule_id", sa.UUID(), nullable=False),
        sa.Column("workflow_version_id", sa.UUID(), nullable=False),
        sa.Column("schedule_revision", sa.Integer(), nullable=False),
        sa.Column("occurrence_key", sa.String(160), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("intended_local_time", sa.DateTime(timezone=False), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("utc_offset_seconds", sa.Integer(), nullable=False),
        sa.Column("fold", sa.Integer(), server_default="0", nullable=False),
        sa.Column("timezone_data_version", sa.String(32), nullable=False),
        sa.Column("misfire_policy", sa.String(24), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("claim_owner_id", sa.UUID(), nullable=True),
        sa.Column("claim_token", sa.UUID(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatch_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatch_attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("p14_idempotency_key", sa.String(200), nullable=False),
        sa.Column("workflow_run_id", sa.UUID(), nullable=True),
        sa.Column("workflow_input", _JSONB, nullable=False),
        sa.Column("error_code", sa.String(96), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.CheckConstraint(
            "state IN ('PENDING','CLAIMED','DISPATCHED','FAILED','SKIPPED','CANCELLED')",
            name="ck_scheduler_occurrence_state",
        ),
        sa.CheckConstraint("fold IN (0,1)", name="ck_scheduler_occurrence_fold"),
        sa.CheckConstraint(
            "schedule_revision >= 1 AND dispatch_attempt_count >= 0",
            name="ck_scheduler_occurrence_attempts",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "schedule_id"],
            ["scheduler_schedules.organization_id", "scheduler_schedules.id"],
            name="fk_scheduler_occurrences_org_schedule",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workflow_version_id"],
            ["workflow_versions.organization_id", "workflow_versions.id"],
            name="fk_scheduler_occurrences_org_workflow_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workflow_run_id"],
            ["workflow_runs.organization_id", "workflow_runs.id"],
            name="fk_scheduler_occurrences_org_workflow_run",
            ondelete="RESTRICT",
        ),
        _org_fk("scheduler_occurrences"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scheduler_occurrences")),
        sa.UniqueConstraint("organization_id", "id", name="uq_scheduler_occurrences_org_id"),
        sa.UniqueConstraint(
            "organization_id",
            "schedule_id",
            "occurrence_key",
            name="uq_scheduler_occurrences_identity",
        ),
        sa.UniqueConstraint(
            "organization_id", "p14_idempotency_key", name="uq_scheduler_occurrences_p14_key"
        ),
    )
    op.create_index(
        "ix_scheduler_occurrences_due",
        "scheduler_occurrences",
        ["organization_id", "state", "scheduled_for"],
    )
    op.create_index(
        "ix_scheduler_occurrences_schedule",
        "scheduler_occurrences",
        ["organization_id", "schedule_id", "created_at"],
    )
    op.create_index(
        "ix_scheduler_occurrences_organization_id", "scheduler_occurrences", ["organization_id"]
    )
    apply_tenant_rls(op, "scheduler_occurrences")

    op.create_table(
        "scheduler_transition_history",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("schedule_id", sa.UUID(), nullable=False),
        sa.Column("occurrence_id", sa.UUID(), nullable=True),
        sa.Column("entity_type", sa.String(16), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column("from_state", sa.String(16), nullable=True),
        sa.Column("to_state", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(96), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("correlation_id", sa.String(128), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.CheckConstraint(
            "entity_type IN ('SCHEDULE','OCCURRENCE')", name="ck_scheduler_transition_entity"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "schedule_id"],
            ["scheduler_schedules.organization_id", "scheduler_schedules.id"],
            name="fk_scheduler_transition_history_org_schedule",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "occurrence_id"],
            ["scheduler_occurrences.organization_id", "scheduler_occurrences.id"],
            name="fk_scheduler_transition_history_org_occurrence",
            ondelete="RESTRICT",
        ),
        _org_fk("scheduler_transition_history"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scheduler_transition_history")),
        sa.UniqueConstraint("organization_id", "id", name="uq_scheduler_transition_history_org_id"),
    )
    op.create_index(
        "ix_scheduler_transition_entity",
        "scheduler_transition_history",
        ["organization_id", "entity_type", "entity_id", "created_at"],
    )
    op.create_index(
        "ix_scheduler_transition_history_organization_id",
        "scheduler_transition_history",
        ["organization_id"],
    )
    apply_tenant_rls(op, "scheduler_transition_history")
    op.execute(
        """
        CREATE FUNCTION nxs_guard_scheduler_occurrence_snapshot() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
          IF ROW(NEW.organization_id, NEW.schedule_id, NEW.workflow_version_id,
                 NEW.schedule_revision, NEW.occurrence_key, NEW.scheduled_for,
                 NEW.intended_local_time, NEW.timezone, NEW.utc_offset_seconds,
                 NEW.fold, NEW.timezone_data_version, NEW.misfire_policy,
                 NEW.p14_idempotency_key, NEW.workflow_input)
             IS DISTINCT FROM
             ROW(OLD.organization_id, OLD.schedule_id, OLD.workflow_version_id,
                 OLD.schedule_revision, OLD.occurrence_key, OLD.scheduled_for,
                 OLD.intended_local_time, OLD.timezone, OLD.utc_offset_seconds,
                 OLD.fold, OLD.timezone_data_version, OLD.misfire_policy,
                 OLD.p14_idempotency_key, OLD.workflow_input) THEN
            RAISE EXCEPTION 'materialized scheduler occurrence semantics are immutable'
              USING ERRCODE = '55000';
          END IF;
          RETURN NEW;
        END; $$
        """
    )
    op.execute(
        "CREATE TRIGGER nxs_immutable_scheduler_occurrence BEFORE UPDATE "
        "ON scheduler_occurrences FOR EACH ROW "
        "EXECUTE FUNCTION nxs_guard_scheduler_occurrence_snapshot()"
    )
    op.execute(
        """
        CREATE FUNCTION nxs_reject_scheduler_history_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
          RAISE EXCEPTION 'scheduler transition history is immutable' USING ERRCODE = '55000';
        END; $$
        """
    )
    op.execute(
        "CREATE TRIGGER nxs_immutable_scheduler_history BEFORE UPDATE OR DELETE "
        "ON scheduler_transition_history FOR EACH ROW "
        "EXECUTE FUNCTION nxs_reject_scheduler_history_mutation()"
    )
    _seed_permissions()


def _seed_permissions() -> None:
    permissions = sa.table(
        "permissions",
        sa.column("id", sa.UUID()),
        sa.column("permission_key", sa.String()),
        sa.column("description", sa.String()),
    )
    op.bulk_insert(
        permissions,
        [
            {"id": PERMISSION_IDS[key], "permission_key": key.value, "description": description}
            for key, description in _PERMISSIONS
        ],
    )
    grants = sa.table(
        "role_permissions", sa.column("role_id", sa.UUID()), sa.column("permission_id", sa.UUID())
    )
    rows = [
        {"role_id": ROLE_IDS[role], "permission_id": PERMISSION_IDS[key]}
        for role in (RoleKey.ORG_OWNER, RoleKey.ORG_ADMIN)
        for key, _ in _PERMISSIONS
    ]
    rows += [
        {"role_id": ROLE_IDS[RoleKey.ORG_MEMBER], "permission_id": PERMISSION_IDS[key]}
        for key in (PermissionKey.SCHEDULE_READ, PermissionKey.SCHEDULE_EXECUTE)
    ]
    op.bulk_insert(grants, rows)


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "('b2000000-0000-7000-8000-000000000033',"
        "'b2000000-0000-7000-8000-000000000034',"
        "'b2000000-0000-7000-8000-000000000035')"
    )
    op.execute("DELETE FROM permissions WHERE permission_key LIKE 'schedule:%'")
    op.execute(
        "DROP TRIGGER IF EXISTS nxs_immutable_scheduler_history ON scheduler_transition_history"
    )
    op.execute("DROP TRIGGER IF EXISTS nxs_immutable_scheduler_occurrence ON scheduler_occurrences")
    op.execute("DROP FUNCTION IF EXISTS nxs_guard_scheduler_occurrence_snapshot()")
    op.execute("DROP FUNCTION IF EXISTS nxs_reject_scheduler_history_mutation()")
    for table in ("scheduler_transition_history", "scheduler_occurrences", "scheduler_schedules"):
        drop_tenant_rls(op, table)
        op.drop_table(table)
