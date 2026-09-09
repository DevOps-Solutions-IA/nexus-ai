"""otp services (NXS-P10: NXS-OTP-001)

``otp_challenges`` is TENANT-OWNED with forced RLS. It binds to its delivery messaging
account and (once delivered) its message with COMPOSITE TENANT-AWARE foreign keys
``(organization_id, <id>) -> (organization_id, id)`` so a challenge can only ever
reference rows in its own Organization. The plaintext OTP is never stored — ``code_hash``
is an HMAC keyed by the configured pepper over the canonical challenge context and the
code.

A PARTIAL UNIQUE INDEX on ``(organization_id, destination_fingerprint, purpose)
WHERE status = 'ACTIVE'`` enforces the "one live code per subject/purpose" policy at the
database, so a concurrent double-issue resolves to exactly one active challenge.

The migration also seeds the P10 permission-catalog delta: owner/admin receive every
``otp:*`` permission; org_member receives ``otp:read``, ``otp:issue`` and ``otp:verify``.

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-09-09 12:10:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from nexus_ai.domain.auth.rbac import PERMISSION_IDS, ROLE_IDS, PermissionKey, RoleKey
from nexus_ai.infrastructure.rls import apply_tenant_rls, drop_tenant_rls

revision: str = "d2e3f4a5b6c7"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.text("now()")

_P10_PERMISSIONS: tuple[tuple[PermissionKey, str], ...] = (
    (PermissionKey.OTP_READ, "Read OTP challenge metadata"),
    (PermissionKey.OTP_ISSUE, "Issue a one-time code and deliver it through a channel"),
    (PermissionKey.OTP_VERIFY, "Verify a submitted one-time code"),
)
_MEMBER_PERMISSIONS = (PermissionKey.OTP_READ, PermissionKey.OTP_ISSUE, PermissionKey.OTP_VERIFY)


def upgrade() -> None:
    op.create_table(
        "otp_challenges",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("subject_type", sa.String(length=16), nullable=False),
        sa.Column("destination", sa.String(length=320), nullable=False),
        sa.Column("destination_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("purpose", sa.String(length=48), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("messaging_account_id", sa.UUID(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("hash_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("delivery_message_id", sa.UUID(), nullable=True),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resend_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.CheckConstraint(
            "status IN ('ACTIVE','VERIFIED','EXPIRED','REVOKED','LOCKED')",
            name="ck_otp_challenges_status_known",
        ),
        sa.CheckConstraint(
            "channel IN ('SMS','EMAIL')", name="ck_otp_challenges_channel_known"
        ),
        sa.CheckConstraint(
            "subject_type IN ('DESTINATION')", name="ck_otp_challenges_subject_type_known"
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_otp_challenges_attempts_non_negative"),
        sa.CheckConstraint(
            "max_attempts BETWEEN 1 AND 10", name="ck_otp_challenges_max_attempts_bounds"
        ),
        sa.CheckConstraint(
            "attempts <= max_attempts", name="ck_otp_challenges_attempts_within_limit"
        ),
        sa.CheckConstraint(
            "expires_at > issued_at", name="ck_otp_challenges_expiry_after_issue"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_otp_challenges_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "messaging_account_id"],
            ["messaging_accounts.organization_id", "messaging_accounts.id"],
            name="fk_otp_challenges_org_messaging_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "delivery_message_id"],
            ["messaging_messages.organization_id", "messaging_messages.id"],
            name="fk_otp_challenges_org_delivery_message",
            ondelete="SET NULL (delivery_message_id)",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_otp_challenges")),
        sa.UniqueConstraint("organization_id", "id", name="uq_otp_challenges_org_id"),
        sa.UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_otp_challenges_org_idempotency_key"
        ),
    )
    op.create_index(
        op.f("ix_otp_challenges_organization_id"),
        "otp_challenges",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_otp_challenges_subject_window",
        "otp_challenges",
        ["organization_id", "destination_fingerprint", "purpose", "issued_at"],
        unique=False,
    )
    op.create_index(
        "ix_otp_challenges_expiry",
        "otp_challenges",
        ["organization_id", "expires_at"],
        unique=False,
    )
    op.create_index(
        "uq_otp_challenges_one_active",
        "otp_challenges",
        ["organization_id", "destination_fingerprint", "purpose"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    apply_tenant_rls(op, "otp_challenges", allow_delete=True)

    _seed_permissions()


def _seed_permissions() -> None:
    permissions_table = sa.table(
        "permissions",
        sa.column("id", sa.UUID()),
        sa.column("permission_key", sa.String()),
        sa.column("description", sa.String()),
    )
    op.bulk_insert(
        permissions_table,
        [
            {"id": PERMISSION_IDS[key], "permission_key": key.value, "description": description}
            for key, description in _P10_PERMISSIONS
        ],
    )
    role_permissions_table = sa.table(
        "role_permissions",
        sa.column("role_id", sa.UUID()),
        sa.column("permission_id", sa.UUID()),
    )
    rows = [
        {"role_id": ROLE_IDS[role], "permission_id": PERMISSION_IDS[key]}
        for role in (RoleKey.ORG_OWNER, RoleKey.ORG_ADMIN)
        for key, _ in _P10_PERMISSIONS
    ] + [
        {"role_id": ROLE_IDS[RoleKey.ORG_MEMBER], "permission_id": PERMISSION_IDS[key]}
        for key in _MEMBER_PERMISSIONS
    ]
    op.bulk_insert(role_permissions_table, rows)


def downgrade() -> None:
    permission_ids = [str(PERMISSION_IDS[key]) for key, _ in _P10_PERMISSIONS]
    op.execute(
        sa.text("DELETE FROM role_permissions WHERE permission_id = ANY(:ids)").bindparams(
            sa.bindparam("ids", value=permission_ids)
        )
    )
    op.execute("DELETE FROM permissions WHERE permission_key LIKE 'otp:%'")
    drop_tenant_rls(op, "otp_challenges")
    op.drop_table("otp_challenges")
