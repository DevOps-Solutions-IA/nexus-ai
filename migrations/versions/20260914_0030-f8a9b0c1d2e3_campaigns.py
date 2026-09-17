"""durable tenant-isolated Campaigns (NXS-P16: NXS-CAMP-001)

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-09-14 00:30:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from nexus_ai.domain.auth.rbac import PERMISSION_IDS, ROLE_IDS, PermissionKey, RoleKey
from nexus_ai.infrastructure.rls import apply_tenant_rls, drop_tenant_rls

revision: str = "f8a9b0c1d2e3"
down_revision: str | None = "e7f8a9b0c1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSONB = postgresql.JSONB(astext_type=sa.Text())
_NOW = sa.text("now()")
_PERMISSIONS = (
    (PermissionKey.CAMPAIGN_READ, "Read campaigns, audiences, runs and history"),
    (
        PermissionKey.CAMPAIGN_EXECUTE,
        "Prepare, schedule, start, pause, resume and cancel campaigns",
    ),
    (
        PermissionKey.CAMPAIGN_CONFIGURE,
        "Create campaign drafts and manage campaign consent/suppression",
    ),
)


def _tenant_table(name: str, *items: object) -> None:
    op.create_table(
        name,
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        *items,
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f(f"fk_{name}_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{name}")),
    )
    op.create_index(op.f(f"ix_{name}_organization_id"), name, ["organization_id"])
    apply_tenant_rls(op, name)


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_customer_identities_org_id", "customer_identities", ["organization_id", "id"]
    )
    _tenant_table(
        "campaigns",
        sa.Column("campaign_key", sa.String(64), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("draft_specification", _JSONB, nullable=False),
        sa.Column("prepared_revision_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint("organization_id", "id", name="uq_campaigns_org_id"),
        sa.UniqueConstraint("organization_id", "campaign_key", name="uq_campaigns_org_key"),
        sa.CheckConstraint(
            "state IN ('DRAFT','PREPARING','READY','SCHEDULED','RUNNING','PAUSED',"
            "'COMPLETED','FAILED','CANCELLED')",
            name="ck_campaigns_state",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_campaigns_revision"),
    )
    op.create_index("ix_campaigns_state", "campaigns", ["organization_id", "state", "created_at"])

    _tenant_table(
        "campaign_revisions",
        sa.Column("campaign_id", sa.UUID(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("release_workflow_version_id", sa.UUID(), nullable=False),
        sa.Column("recipient_workflow_version_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("specification", _JSONB, nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint("organization_id", "id", name="uq_campaign_revisions_org_id"),
        sa.UniqueConstraint(
            "organization_id", "campaign_id", "revision_number", name="uq_campaign_revision_number"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "campaign_id"],
            ["campaigns.organization_id", "campaigns.id"],
            name="fk_campaign_revisions_org_campaign",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "release_workflow_version_id"],
            ["workflow_versions.organization_id", "workflow_versions.id"],
            name="fk_campaign_revisions_org_release_workflow",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "recipient_workflow_version_id"],
            ["workflow_versions.organization_id", "workflow_versions.id"],
            name="fk_campaign_revisions_org_recipient_workflow",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "account_id"],
            ["messaging_accounts.organization_id", "messaging_accounts.id"],
            name="fk_campaign_revisions_org_account",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_campaign_revisions_campaign",
        "campaign_revisions",
        ["organization_id", "campaign_id", "revision_number"],
    )

    _tenant_table(
        "campaign_audience_snapshots",
        sa.Column("campaign_revision_id", sa.UUID(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_reference", sa.String(200), nullable=True),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("cursor", sa.Integer(), server_default="0", nullable=False),
        sa.Column("resolved_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("eligible_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("rejected_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint("organization_id", "id", name="uq_campaign_audience_snapshots_org_id"),
        sa.UniqueConstraint(
            "organization_id", "campaign_revision_id", name="uq_campaign_audience_revision"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "campaign_revision_id"],
            ["campaign_revisions.organization_id", "campaign_revisions.id"],
            name="fk_campaign_audience_org_revision",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "state IN ('OPEN','SEALED','FAILED')", name="ck_campaign_audience_state"
        ),
        sa.CheckConstraint(
            "cursor >= 0 AND resolved_count >= 0 AND eligible_count >= 0 AND rejected_count >= 0",
            name="ck_campaign_audience_counts",
        ),
    )

    _tenant_table(
        "campaign_recipients",
        sa.Column("snapshot_id", sa.UUID(), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("identity_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("destination_fingerprint", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("eligibility_reason", sa.String(48), nullable=False),
        sa.Column("next_eligible_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evaluated_consent_epoch", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "evaluated_suppression_epoch", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint("organization_id", "id", name="uq_campaign_recipients_org_id"),
        sa.UniqueConstraint(
            "organization_id", "snapshot_id", "customer_id", "channel", name="uq_campaign_recipient"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "snapshot_id"],
            ["campaign_audience_snapshots.organization_id", "campaign_audience_snapshots.id"],
            name="fk_campaign_recipients_org_snapshot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_campaign_recipients_org_customer",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "identity_id"],
            ["customer_identities.organization_id", "customer_identities.id"],
            name="fk_campaign_recipients_org_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["conversations.organization_id", "conversations.id"],
            name="fk_campaign_recipients_org_conversation",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "state IN ('PENDING','ELIGIBLE','DEFERRED','SUPPRESSED','CANCELLED')",
            name="ck_campaign_recipients_state",
        ),
    )
    op.create_index(
        "ix_campaign_recipients_work",
        "campaign_recipients",
        ["organization_id", "snapshot_id", "state", "next_eligible_at"],
    )

    _tenant_table(
        "campaign_runs",
        sa.Column("campaign_id", sa.UUID(), nullable=False),
        sa.Column("campaign_revision_id", sa.UUID(), nullable=False),
        sa.Column("audience_snapshot_id", sa.UUID(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("release_workflow_run_id", sa.UUID(), nullable=True),
        sa.Column("schedule_id", sa.UUID(), nullable=True),
        sa.Column("claimed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("dispatched_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("suppressed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint("organization_id", "id", name="uq_campaign_runs_org_id"),
        sa.UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_campaign_runs_idempotency"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "campaign_id"],
            ["campaigns.organization_id", "campaigns.id"],
            name="fk_campaign_runs_org_campaign",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "campaign_revision_id"],
            ["campaign_revisions.organization_id", "campaign_revisions.id"],
            name="fk_campaign_runs_org_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "audience_snapshot_id"],
            ["campaign_audience_snapshots.organization_id", "campaign_audience_snapshots.id"],
            name="fk_campaign_runs_org_snapshot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "release_workflow_run_id"],
            ["workflow_runs.organization_id", "workflow_runs.id"],
            name="fk_campaign_runs_org_release_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "schedule_id"],
            ["scheduler_schedules.organization_id", "scheduler_schedules.id"],
            name="fk_campaign_runs_org_schedule",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "state IN ('PENDING_RELEASE','RUNNING','PAUSED','COMPLETED','FAILED','CANCELLED')",
            name="ck_campaign_runs_state",
        ),
    )
    op.create_index(
        "ix_campaign_runs_state", "campaign_runs", ["organization_id", "state", "created_at"]
    )

    _tenant_table(
        "campaign_recipient_attempts",
        sa.Column("campaign_run_id", sa.UUID(), nullable=False),
        sa.Column("recipient_id", sa.UUID(), nullable=False),
        sa.Column("logical_identity", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("attempt_number", sa.Integer(), server_default="1", nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=True),
        sa.Column("claim_token", sa.UUID(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("p14_idempotency_key", sa.String(200), nullable=False),
        sa.Column("workflow_run_id", sa.UUID(), nullable=True),
        sa.Column("p09_idempotency_key", sa.String(200), nullable=False),
        sa.Column("message_id", sa.UUID(), nullable=True),
        sa.Column("error_code", sa.String(96), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint("organization_id", "id", name="uq_campaign_attempts_org_id"),
        sa.UniqueConstraint(
            "organization_id", "logical_identity", name="uq_campaign_attempt_identity"
        ),
        sa.UniqueConstraint(
            "organization_id", "p14_idempotency_key", name="uq_campaign_attempt_p14_key"
        ),
        sa.UniqueConstraint(
            "organization_id", "p09_idempotency_key", name="uq_campaign_attempt_p09_key"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "campaign_run_id"],
            ["campaign_runs.organization_id", "campaign_runs.id"],
            name="fk_campaign_attempts_org_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "recipient_id"],
            ["campaign_recipients.organization_id", "campaign_recipients.id"],
            name="fk_campaign_attempts_org_recipient",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workflow_run_id"],
            ["workflow_runs.organization_id", "workflow_runs.id"],
            name="fk_campaign_attempts_org_workflow_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "message_id"],
            ["messaging_messages.organization_id", "messaging_messages.id"],
            name="fk_campaign_attempts_org_message",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("attempt_number >= 1", name="ck_campaign_attempt_number"),
        sa.CheckConstraint(
            "state IN ('PENDING','CLAIMED','WORKFLOW_RUNNING','READY_TO_SEND',"
            "'DISPATCH_AUTHORIZED','DISPATCHED','SUPPRESSED','FAILED','CANCELLED')",
            name="ck_campaign_attempt_state",
        ),
    )
    op.create_index(
        "ix_campaign_attempts_claim",
        "campaign_recipient_attempts",
        ["organization_id", "campaign_run_id", "state", "created_at"],
    )

    _tenant_table(
        "campaign_send_permits",
        sa.Column("recipient_attempt_id", sa.UUID(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("permit_token", sa.UUID(), nullable=False),
        sa.Column("consent_epoch", sa.BigInteger(), nullable=False),
        sa.Column("suppression_epoch", sa.BigInteger(), nullable=False),
        sa.Column("p09_idempotency_key", sa.String(200), nullable=False),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message_id", sa.UUID(), nullable=True),
        sa.UniqueConstraint("permit_token", name=op.f("uq_campaign_send_permits_permit_token")),
        sa.UniqueConstraint("organization_id", "id", name="uq_campaign_permits_org_id"),
        sa.UniqueConstraint(
            "organization_id", "recipient_attempt_id", name="uq_campaign_permit_attempt"
        ),
        sa.UniqueConstraint(
            "organization_id", "p09_idempotency_key", name="uq_campaign_permit_p09_key"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "recipient_attempt_id"],
            ["campaign_recipient_attempts.organization_id", "campaign_recipient_attempts.id"],
            name="fk_campaign_permits_org_attempt",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "message_id"],
            ["messaging_messages.organization_id", "messaging_messages.id"],
            name="fk_campaign_permits_org_message",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "state IN ('PENDING','AUTHORIZED','CONSUMED','FAILED','EXPIRED')",
            name="ck_campaign_permit_state",
        ),
    )

    _tenant_table(
        "campaign_contact_preferences",
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("identity_id", sa.UUID(), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("consent_granted", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("unsubscribed", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("do_not_contact", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("consent_epoch", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("evidence_ref", sa.String(200), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint("organization_id", "id", name="uq_campaign_preferences_org_id"),
        sa.UniqueConstraint(
            "organization_id", "identity_id", "channel", name="uq_campaign_preference_identity"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_campaign_preferences_org_customer",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "identity_id"],
            ["customer_identities.organization_id", "customer_identities.id"],
            name="fk_campaign_preferences_org_identity",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("consent_epoch >= 1", name="ck_campaign_preference_epoch"),
    )

    _tenant_table(
        "campaign_policy_epochs",
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("suppression_epoch", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint("organization_id", "id", name="uq_campaign_policy_epochs_org_id"),
        sa.UniqueConstraint("organization_id", "channel", name="uq_campaign_policy_epoch_channel"),
        sa.CheckConstraint("suppression_epoch >= 1", name="ck_campaign_policy_epoch"),
    )

    _tenant_table(
        "campaign_suppressions",
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("campaign_id", sa.UUID(), nullable=True),
        sa.Column("customer_id", sa.UUID(), nullable=True),
        sa.Column("identity_id", sa.UUID(), nullable=True),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint("organization_id", "id", name="uq_campaign_suppressions_org_id"),
        sa.ForeignKeyConstraint(
            ["organization_id", "campaign_id"],
            ["campaigns.organization_id", "campaigns.id"],
            name="fk_campaign_suppressions_org_campaign",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_campaign_suppressions_org_customer",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "identity_id"],
            ["customer_identities.organization_id", "customer_identities.id"],
            name="fk_campaign_suppressions_org_identity",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "scope IN ('GLOBAL','CAMPAIGN','CUSTOMER','IDENTITY')",
            name="ck_campaign_suppression_scope",
        ),
    )
    op.create_index(
        "ix_campaign_suppressions_lookup",
        "campaign_suppressions",
        ["organization_id", "channel", "active", "scope"],
    )

    _tenant_table(
        "campaign_throttle_windows",
        sa.Column("campaign_run_id", sa.UUID(), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reserved_count", sa.Integer(), server_default="0", nullable=False),
        sa.UniqueConstraint("organization_id", "id", name="uq_campaign_throttle_windows_org_id"),
        sa.UniqueConstraint(
            "organization_id",
            "campaign_run_id",
            "channel",
            "window_start",
            name="uq_campaign_throttle_window",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "campaign_run_id"],
            ["campaign_runs.organization_id", "campaign_runs.id"],
            name="fk_campaign_throttle_org_run",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("reserved_count >= 0", name="ck_campaign_throttle_count"),
    )

    _tenant_table(
        "campaign_transition_history",
        sa.Column("entity_type", sa.String(16), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column("from_state", sa.String(24), nullable=True),
        sa.Column("to_state", sa.String(24), nullable=False),
        sa.Column("reason_code", sa.String(96), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("correlation_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint("organization_id", "id", name="uq_campaign_history_org_id"),
        sa.CheckConstraint(
            "entity_type IN ('CAMPAIGN','RUN','RECIPIENT','ATTEMPT','PERMIT')",
            name="ck_campaign_history_entity",
        ),
    )
    op.create_index(
        "ix_campaign_history_entity",
        "campaign_transition_history",
        ["organization_id", "entity_type", "entity_id", "created_at"],
    )

    op.execute("""
      CREATE FUNCTION nxs_guard_campaign_revision() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'campaign revisions are immutable' USING ERRCODE='55000'; END; $$
    """)
    op.execute(
        "CREATE TRIGGER nxs_immutable_campaign_revision BEFORE UPDATE OR DELETE "
        "ON campaign_revisions FOR EACH ROW EXECUTE FUNCTION nxs_guard_campaign_revision()"
    )
    op.execute("""
      CREATE FUNCTION nxs_guard_sealed_campaign_audience() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF OLD.state = 'SEALED' THEN
          RAISE EXCEPTION 'sealed campaign audiences are immutable' USING ERRCODE='55000';
        END IF;
        RETURN NEW;
      END; $$
    """)
    op.execute(
        "CREATE TRIGGER nxs_immutable_sealed_campaign_audience BEFORE UPDATE OR DELETE "
        "ON campaign_audience_snapshots FOR EACH ROW "
        "EXECUTE FUNCTION nxs_guard_sealed_campaign_audience()"
    )
    op.execute("""
      CREATE FUNCTION nxs_guard_campaign_recipient_identity() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP = 'DELETE' OR
           (OLD.organization_id, OLD.snapshot_id, OLD.customer_id, OLD.identity_id,
            OLD.conversation_id, OLD.channel, OLD.destination_fingerprint)
           IS DISTINCT FROM
           (NEW.organization_id, NEW.snapshot_id, NEW.customer_id, NEW.identity_id,
            NEW.conversation_id, NEW.channel, NEW.destination_fingerprint) THEN
          RAISE EXCEPTION 'campaign recipient identity is immutable' USING ERRCODE='55000';
        END IF;
        RETURN NEW;
      END; $$
    """)
    op.execute(
        "CREATE TRIGGER nxs_immutable_campaign_recipient_identity BEFORE UPDATE OR DELETE "
        "ON campaign_recipients FOR EACH ROW "
        "EXECUTE FUNCTION nxs_guard_campaign_recipient_identity()"
    )
    op.execute("""
      CREATE FUNCTION nxs_guard_campaign_history() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        RAISE EXCEPTION 'campaign transition history is immutable' USING ERRCODE='55000';
      END; $$
    """)
    op.execute(
        "CREATE TRIGGER nxs_immutable_campaign_history BEFORE UPDATE OR DELETE "
        "ON campaign_transition_history FOR EACH ROW EXECUTE FUNCTION nxs_guard_campaign_history()"
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
        for key in (PermissionKey.CAMPAIGN_READ, PermissionKey.CAMPAIGN_EXECUTE)
    ]
    op.bulk_insert(grants, rows)


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "('b2000000-0000-7000-8000-000000000036',"
        "'b2000000-0000-7000-8000-000000000037',"
        "'b2000000-0000-7000-8000-000000000038')"
    )
    op.execute("DELETE FROM permissions WHERE permission_key LIKE 'campaign:%'")
    op.execute(
        "DROP TRIGGER IF EXISTS nxs_immutable_campaign_history ON campaign_transition_history"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS nxs_immutable_campaign_recipient_identity ON campaign_recipients"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS nxs_immutable_sealed_campaign_audience "
        "ON campaign_audience_snapshots"
    )
    op.execute("DROP TRIGGER IF EXISTS nxs_immutable_campaign_revision ON campaign_revisions")
    op.execute("DROP FUNCTION IF EXISTS nxs_guard_campaign_history()")
    op.execute("DROP FUNCTION IF EXISTS nxs_guard_campaign_recipient_identity()")
    op.execute("DROP FUNCTION IF EXISTS nxs_guard_sealed_campaign_audience()")
    op.execute("DROP FUNCTION IF EXISTS nxs_guard_campaign_revision()")
    for table in (
        "campaign_transition_history",
        "campaign_throttle_windows",
        "campaign_suppressions",
        "campaign_policy_epochs",
        "campaign_contact_preferences",
        "campaign_send_permits",
        "campaign_recipient_attempts",
        "campaign_runs",
        "campaign_recipients",
        "campaign_audience_snapshots",
        "campaign_revisions",
        "campaigns",
    ):
        drop_tenant_rls(op, table)
        op.drop_table(table)
    op.drop_constraint("uq_customer_identities_org_id", "customer_identities", type_="unique")
