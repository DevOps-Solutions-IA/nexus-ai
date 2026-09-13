"""durable tenant-isolated workflow engine (NXS-P14: NXS-WF-001)

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-09-13 04:15:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from nexus_ai.domain.auth.rbac import PERMISSION_IDS, ROLE_IDS, PermissionKey, RoleKey
from nexus_ai.infrastructure.rls import apply_tenant_rls, drop_tenant_rls

revision: str = "c5d6e7f8a9b0"
down_revision: str | None = "b4c5d6e7f8a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSONB = postgresql.JSONB(astext_type=sa.Text())
_NOW = sa.text("now()")
_PERMISSIONS = (
    (PermissionKey.WORKFLOW_READ, "Read workflow definitions, versions, runs and history"),
    (PermissionKey.WORKFLOW_EXECUTE, "Start, pause, resume and cancel workflow runs"),
    (PermissionKey.WORKFLOW_CONFIGURE, "Create, edit and publish workflow definitions"),
)


def _org_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id"],
        ["organizations.id"],
        name=op.f(f"fk_{table}_organization_id_organizations"),
        ondelete="RESTRICT",
    )


def _timestamps(*, mutable: bool = True) -> list[sa.Column]:
    columns = [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False)
    ]
    if mutable:
        columns.append(
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False)
        )
    return columns


def upgrade() -> None:
    op.create_table(
        "workflow_definitions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("workflow_key", sa.String(64), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(800), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("draft_steps", _JSONB, nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('DRAFT','ACTIVE','ARCHIVED')", name="ck_workflow_definition_status"
        ),
        sa.CheckConstraint("revision >= 1", name="ck_workflow_definition_revision"),
        _org_fk("workflow_definitions"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_definitions")),
        sa.UniqueConstraint("organization_id", "id", name="uq_workflow_definitions_org_id"),
        sa.UniqueConstraint(
            "organization_id", "workflow_key", name="uq_workflow_definitions_org_key"
        ),
    )
    op.create_index(
        "ix_workflow_definitions_org_status", "workflow_definitions", ["organization_id", "status"]
    )
    op.create_index(
        "ix_workflow_definitions_organization_id",
        "workflow_definitions",
        ["organization_id"],
    )
    apply_tenant_rls(op, "workflow_definitions")

    op.create_table(
        "workflow_versions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("definition_id", sa.UUID(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        *_timestamps(mutable=False),
        sa.CheckConstraint("version_number >= 1", name="ck_workflow_version_positive"),
        sa.ForeignKeyConstraint(
            ["organization_id", "definition_id"],
            ["workflow_definitions.organization_id", "workflow_definitions.id"],
            name="fk_workflow_versions_org_definition",
            ondelete="RESTRICT",
        ),
        _org_fk("workflow_versions"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_versions")),
        sa.UniqueConstraint("organization_id", "id", name="uq_workflow_versions_org_id"),
        sa.UniqueConstraint(
            "organization_id", "definition_id", "version_number", name="uq_workflow_versions_number"
        ),
    )
    op.create_index(
        "ix_workflow_versions_definition", "workflow_versions", ["organization_id", "definition_id"]
    )
    op.create_index(
        "ix_workflow_versions_organization_id", "workflow_versions", ["organization_id"]
    )
    apply_tenant_rls(op, "workflow_versions")

    op.create_table(
        "workflow_version_steps",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("version_id", sa.UUID(), nullable=False),
        sa.Column("step_key", sa.String(64), nullable=False),
        sa.Column("step_type", sa.String(16), nullable=False),
        sa.Column("dependencies", _JSONB, nullable=False),
        sa.Column("configuration", _JSONB, nullable=False),
        sa.Column("retry_policy", _JSONB, nullable=False),
        sa.Column("topological_order", sa.Integer(), nullable=False),
        *_timestamps(mutable=False),
        sa.CheckConstraint(
            "step_type IN ('TOOL','AGENT','CONDITION','NOOP')", name="ck_workflow_version_step_type"
        ),
        sa.CheckConstraint("topological_order >= 0", name="ck_workflow_step_order"),
        sa.ForeignKeyConstraint(
            ["organization_id", "version_id"],
            ["workflow_versions.organization_id", "workflow_versions.id"],
            name="fk_workflow_version_steps_org_version",
            ondelete="RESTRICT",
        ),
        _org_fk("workflow_version_steps"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_version_steps")),
        sa.UniqueConstraint("organization_id", "id", name="uq_workflow_version_steps_org_id"),
        sa.UniqueConstraint(
            "organization_id", "version_id", "step_key", name="uq_workflow_version_steps_key"
        ),
    )
    op.create_index(
        "ix_workflow_version_steps_version",
        "workflow_version_steps",
        ["organization_id", "version_id", "topological_order"],
    )
    op.create_index(
        "ix_workflow_version_steps_organization_id",
        "workflow_version_steps",
        ["organization_id"],
    )
    apply_tenant_rls(op, "workflow_version_steps")

    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("definition_id", sa.UUID(), nullable=False),
        sa.Column("version_id", sa.UUID(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("state_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("input_payload", _JSONB, nullable=False),
        sa.Column("output_payload", _JSONB, nullable=True),
        sa.Column("idempotency_key", sa.String(200), nullable=True),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("correlation_id", sa.String(128), nullable=True),
        sa.Column("error_code", sa.String(96), nullable=True),
        sa.Column("terminal_reason", sa.String(200), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "state IN ('PENDING','RUNNING','PAUSED','COMPLETED','FAILED','CANCELLED')",
            name="ck_workflow_run_state",
        ),
        sa.CheckConstraint("state_version >= 1", name="ck_workflow_run_state_version"),
        sa.ForeignKeyConstraint(
            ["organization_id", "definition_id"],
            ["workflow_definitions.organization_id", "workflow_definitions.id"],
            name="fk_workflow_runs_org_definition",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "version_id"],
            ["workflow_versions.organization_id", "workflow_versions.id"],
            name="fk_workflow_runs_org_version",
            ondelete="RESTRICT",
        ),
        _org_fk("workflow_runs"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_runs")),
        sa.UniqueConstraint("organization_id", "id", name="uq_workflow_runs_org_id"),
        sa.UniqueConstraint(
            "organization_id", "version_id", "idempotency_key", name="uq_workflow_runs_idempotency"
        ),
    )
    op.create_index(
        "ix_workflow_runs_org_state", "workflow_runs", ["organization_id", "state", "created_at"]
    )
    op.create_index("ix_workflow_runs_organization_id", "workflow_runs", ["organization_id"])
    apply_tenant_rls(op, "workflow_runs")

    op.create_table(
        "workflow_step_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("version_step_id", sa.UUID(), nullable=False),
        sa.Column("step_key", sa.String(64), nullable=False),
        sa.Column("step_type", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("execution_owner_id", sa.UUID(), nullable=True),
        sa.Column("claim_token", sa.UUID(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_eligible_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_payload", _JSONB, nullable=False),
        sa.Column("output_payload", _JSONB, nullable=True),
        sa.Column("error_code", sa.String(96), nullable=True),
        sa.Column("external_execution_ref", sa.String(160), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "state IN ('PENDING','READY','RUNNING','COMPLETED','FAILED','SKIPPED','CANCELLED')",
            name="ck_workflow_step_run_state",
        ),
        sa.CheckConstraint(
            "step_type IN ('TOOL','AGENT','CONDITION','NOOP')", name="ck_workflow_step_run_type"
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts >= 1", name="ck_workflow_step_attempts"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "run_id"],
            ["workflow_runs.organization_id", "workflow_runs.id"],
            name="fk_workflow_step_runs_org_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "version_step_id"],
            ["workflow_version_steps.organization_id", "workflow_version_steps.id"],
            name="fk_workflow_step_runs_org_version_step",
            ondelete="RESTRICT",
        ),
        _org_fk("workflow_step_runs"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_step_runs")),
        sa.UniqueConstraint("organization_id", "id", name="uq_workflow_step_runs_org_id"),
        sa.UniqueConstraint(
            "organization_id", "run_id", "step_key", name="uq_workflow_step_runs_key"
        ),
    )
    op.create_index(
        "ix_workflow_step_runs_ready",
        "workflow_step_runs",
        ["organization_id", "run_id", "state", "next_eligible_at"],
    )
    op.create_index(
        "ix_workflow_step_runs_organization_id",
        "workflow_step_runs",
        ["organization_id"],
    )
    apply_tenant_rls(op, "workflow_step_runs")

    op.create_table(
        "workflow_transition_history",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("entity_type", sa.String(24), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column("from_state", sa.String(16), nullable=True),
        sa.Column("to_state", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(96), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("correlation_id", sa.String(128), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        *_timestamps(mutable=False),
        sa.CheckConstraint(
            "entity_type IN ('WORKFLOW_RUN','WORKFLOW_STEP_RUN')",
            name="ck_workflow_transition_entity_type",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "run_id"],
            ["workflow_runs.organization_id", "workflow_runs.id"],
            name="fk_workflow_transition_history_org_run",
            ondelete="RESTRICT",
        ),
        _org_fk("workflow_transition_history"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_transition_history")),
        sa.UniqueConstraint("organization_id", "id", name="uq_workflow_transition_history_org_id"),
    )
    op.create_index(
        "ix_workflow_transition_run",
        "workflow_transition_history",
        ["organization_id", "run_id", "created_at"],
    )
    op.create_index(
        "ix_workflow_transition_history_organization_id",
        "workflow_transition_history",
        ["organization_id"],
    )
    apply_tenant_rls(op, "workflow_transition_history")
    op.execute(
        """
        CREATE FUNCTION nxs_reject_immutable_workflow_row() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'published workflow records and transition history are immutable'
            USING ERRCODE = '55000';
        END;
        $$
        """
    )
    for table in (
        "workflow_versions",
        "workflow_version_steps",
        "workflow_transition_history",
    ):
        op.execute(
            f"CREATE TRIGGER nxs_immutable_workflow_row BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION nxs_reject_immutable_workflow_row()"
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
        for key in (PermissionKey.WORKFLOW_READ, PermissionKey.WORKFLOW_EXECUTE)
    ]
    op.bulk_insert(grants, rows)


def downgrade() -> None:
    ids = [str(PERMISSION_IDS[key]) for key, _ in _PERMISSIONS]
    op.execute(
        sa.text("DELETE FROM role_permissions WHERE permission_id = ANY(:ids)").bindparams(
            sa.bindparam("ids", value=ids)
        )
    )
    op.execute("DELETE FROM permissions WHERE permission_key LIKE 'workflow:%'")
    for table in (
        "workflow_versions",
        "workflow_version_steps",
        "workflow_transition_history",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS nxs_immutable_workflow_row ON {table}")
    for table in (
        "workflow_transition_history",
        "workflow_step_runs",
        "workflow_runs",
        "workflow_version_steps",
        "workflow_versions",
        "workflow_definitions",
    ):
        drop_tenant_rls(op, table)
        op.drop_table(table)
    op.execute("DROP FUNCTION IF EXISTS nxs_reject_immutable_workflow_row()")
