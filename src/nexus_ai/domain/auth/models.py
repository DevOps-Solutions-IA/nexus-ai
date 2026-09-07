"""Authentication persistence models (NXS-AUTH-001..006).

Classification is conscious and documented in ADR-0038:

* GLOBAL identity plane (no RLS): ``users``, ``user_credentials``, ``roles``,
  ``permissions``, ``role_permissions`` — shared platform catalogs and per-user
  identity data. Authorization for tenant access NEVER derives from these tables
  alone; it flows through tenant-scoped memberships and role assignments.
* TENANT-OWNED (forced RLS): ``memberships``, ``role_assignments``,
  ``refresh_sessions`` — rows carry ``organization_id`` and are only visible inside
  the transaction-local tenant scope, plus the memberships self-visibility policy for
  the identity plane (principal GUC, no org scope bound).
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from nexus_ai.infrastructure.orm import Base, TenantOwnedMixin


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class UserRecord(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
        CheckConstraint("status IN ('ACTIVE', 'SUSPENDED')", name="status_known"),
        CheckConstraint("version >= 1", name="version_positive"),
        Index("ix_users_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    email_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
        onupdate=_utcnow,
    )


class UserCredentialRecord(Base):
    """One row per credential version: history is kept, the newest version verifies."""

    __tablename__ = "user_credentials"
    __table_args__ = (
        CheckConstraint("credential_version >= 1", name="credential_version_positive"),
        CheckConstraint("algorithm = 'argon2id'", name="algorithm_known"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    credential_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    algorithm: Mapped[str] = mapped_column(String(32), nullable=False)
    parameters: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
        onupdate=_utcnow,
    )


class MembershipRecord(TenantOwnedMixin, Base):
    """User-organization membership. One row per (organization, user) pair — status
    transitions mutate that single row, so duplicate active membership ambiguity is
    structurally impossible."""

    __tablename__ = "memberships"
    __table_args__ = (  # type: ignore[assignment]  # mixin dict + tuple merge (P02 pattern)
        UniqueConstraint("organization_id", "user_id", name="uq_memberships_org_user"),
        CheckConstraint("status IN ('ACTIVE', 'SUSPENDED', 'REVOKED')", name="status_known"),
        Index("ix_memberships_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
        onupdate=_utcnow,
    )
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RefreshSessionRecord(TenantOwnedMixin, Base):
    """Server-side refresh session state. ``token_hash`` holds the SHA-256 of the
    current refresh token — the token itself is never stored. Rotation mutates this row
    in place under a conditional UPDATE; a stale presenter loses the race and is treated
    as replay."""

    __tablename__ = "refresh_sessions"
    __table_args__ = (  # type: ignore[assignment]  # mixin dict + tuple merge (P02 pattern)
        UniqueConstraint("token_hash", name="uq_refresh_sessions_token_hash"),
        CheckConstraint("generation >= 1", name="generation_positive"),
        Index("ix_refresh_sessions_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
        onupdate=_utcnow,
    )
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RoleRecord(Base):
    """Global role catalog (definitions are platform-wide, assignments are not)."""

    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("role_key", name="uq_roles_role_key"),)

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    role_key: Mapped[str] = mapped_column(String(48), nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class PermissionRecord(Base):
    """Global permission catalog with stable ``permission_key`` identifiers."""

    __tablename__ = "permissions"
    __table_args__ = (UniqueConstraint("permission_key", name="uq_permissions_permission_key"),)

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    permission_key: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class RolePermissionRecord(Base):
    """Global role → permission catalog mapping. The composite primary key IS the
    uniqueness guarantee — no redundant UNIQUE constraint (PostgreSQL 17 would absorb
    it into the PK index anyway)."""

    __tablename__ = "role_permissions"

    role_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("roles.id", ondelete="RESTRICT"), primary_key=True
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("permissions.id", ondelete="RESTRICT"), primary_key=True
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class RoleAssignmentRecord(TenantOwnedMixin, Base):
    """Organization-scoped role assignment — the RBAC source of truth."""

    __tablename__ = "role_assignments"
    __table_args__ = (  # type: ignore[assignment]  # mixin dict + tuple merge (P02 pattern)
        UniqueConstraint(
            "organization_id", "user_id", "role_id", name="uq_role_assignments_org_user_role"
        ),
        CheckConstraint("status IN ('ACTIVE', 'SUSPENDED')", name="status_known"),
        Index("ix_role_assignments_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
        onupdate=_utcnow,
    )
