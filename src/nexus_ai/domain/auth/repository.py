"""Authentication persistence access (NXS-AUTH-001..006).

Two scoping contracts, explicit in the constructor types:

* global repositories (``UserRepository``, ``CredentialRepository``) take a plain
  ``AsyncSession`` — they operate on the global identity plane, which has no tenant
  scope by classification.
* tenant repositories (``MembershipRepository``, ``RefreshSessionRepository``,
  ``RoleAssignmentRepository``) take a ``TenantSession`` — every query is confined to
  the bound Organization by forced Row-Level Security, whatever the WHERE clause says.

The memberships self-visibility policy (principal GUC, no org scope bound) powers the
identity-plane lookup ``MembershipRepository.for_user`` when it is constructed from a
``Database.principal_session`` — that is the ONLY path that reads a caller's own
memberships across Organizations.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai.core.errors import (
    MembershipConflictError,
    OrganizationNotFoundError,
    TenantScopeMismatchError,
    UserConflictError,
)
from nexus_ai.domain.auth.entities import (
    Membership,
    MembershipStatus,
    RoleAssignmentStatus,
    User,
    UserStatus,
)
from nexus_ai.domain.auth.models import (
    MembershipRecord,
    RefreshSessionRecord,
    RoleAssignmentRecord,
    RoleRecord,
    UserCredentialRecord,
    UserRecord,
)
from nexus_ai.infrastructure.tenant_session import TenantSession


def _to_user(row: UserRecord) -> User:
    return User(
        id=row.id,
        email=row.email,
        email_verified=row.email_verified,
        display_name=row.display_name,
        status=UserStatus(row.status),
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_membership(row: MembershipRecord) -> Membership:
    return Membership(
        id=row.id,
        organization_id=row.organization_id,
        user_id=row.user_id,
        status=MembershipStatus(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
        revoked_at=row.revoked_at,
    )


class UserRepository:
    """Global identity-plane persistence. No tenant scope exists here by design."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def by_email(self, email: str) -> User | None:
        row = (
            (await self._session.execute(select(UserRecord).where(UserRecord.email == email)))
            .scalars()
            .one_or_none()
        )
        return None if row is None else _to_user(row)

    async def by_id(self, user_id: uuid.UUID) -> User | None:
        row = await self._session.get(UserRecord, user_id)
        return None if row is None else _to_user(row)

    async def insert(
        self,
        *,
        user_id: uuid.UUID,
        email: str,
        display_name: str | None,
        email_verified: bool,
    ) -> User:
        now = dt.datetime.now(dt.UTC)
        record = UserRecord(
            id=user_id,
            email=email,
            email_verified=email_verified,
            display_name=display_name,
            status=UserStatus.ACTIVE.value,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise UserConflictError("A user with that email already exists.", cause=exc) from exc
        await self._session.refresh(record)
        return _to_user(record)

    async def set_suspended(self, user_id: uuid.UUID, suspended: bool) -> None:
        await self._session.execute(
            update(UserRecord)
            .where(UserRecord.id == user_id)
            .values(
                status=UserStatus.SUSPENDED.value if suspended else UserStatus.ACTIVE.value,
                version=UserRecord.version + 1,
                updated_at=dt.datetime.now(dt.UTC),
            )
        )


class CredentialRepository:
    """Global credential persistence. The latest version verifies; history is kept."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_latest(self, user_id: uuid.UUID) -> UserCredentialRecord | None:
        return (
            (
                await self._session.execute(
                    select(UserCredentialRecord)
                    .where(UserCredentialRecord.user_id == user_id)
                    .order_by(UserCredentialRecord.credential_version.desc())
                    .limit(1)
                )
            )
            .scalars()
            .one_or_none()
        )

    async def insert(
        self,
        *,
        user_id: uuid.UUID,
        credential_version: int,
        algorithm: str,
        parameters: dict[str, Any],
        password_hash: str,
    ) -> None:
        self._session.add(
            UserCredentialRecord(
                user_id=user_id,
                credential_version=credential_version,
                algorithm=algorithm,
                parameters=parameters,
                password_hash=password_hash,
            )
        )
        await self._session.flush()


class MembershipRepository:
    """Membership persistence. Scope depends on the session it was constructed with:

    * ``TenantSession`` → rows of the bound Organization (forced RLS).
    * ``Database.principal_session`` → exactly the caller's own rows (self policy).
    """

    def __init__(self, session: AsyncSession | TenantSession) -> None:
        if isinstance(session, TenantSession):
            self._tenant_org: uuid.UUID | None = session.organization_id
            self._session = session.session
        else:
            self._tenant_org = None
            self._session = session

    async def for_user(self, user_id: uuid.UUID) -> list[Membership]:
        rows = (
            await self._session.execute(
                select(MembershipRecord)
                .where(MembershipRecord.user_id == user_id)
                .order_by(MembershipRecord.created_at)
            )
        ).scalars()
        return [_to_membership(row) for row in rows]

    async def for_user_in_organization(
        self, user_id: uuid.UUID, organization_id: uuid.UUID
    ) -> Membership | None:
        row = (
            (
                await self._session.execute(
                    select(MembershipRecord).where(
                        MembershipRecord.user_id == user_id,
                        MembershipRecord.organization_id == organization_id,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )
        return None if row is None else _to_membership(row)

    async def insert(
        self,
        *,
        membership_id: uuid.UUID,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Membership:
        """Create an ACTIVE membership row. RLS's WITH CHECK binds the insert to the
        transaction-local Organization — a cross-tenant insert fails at the database."""
        if self._tenant_org is not None and organization_id != self._tenant_org:
            raise TenantScopeMismatchError("Membership id does not match the bound tenant scope.")
        now = dt.datetime.now(dt.UTC)
        record = MembershipRecord(
            id=membership_id,
            organization_id=organization_id,
            user_id=user_id,
            status=MembershipStatus.ACTIVE.value,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise MembershipConflictError(
                "That user already has a membership in this Organization.", cause=exc
            ) from exc
        await self._session.refresh(record)
        return _to_membership(record)

    async def set_status(
        self, membership_id: uuid.UUID, status: MembershipStatus
    ) -> Membership | None:
        changes: dict[str, Any] = {
            "status": status.value,
            "updated_at": dt.datetime.now(dt.UTC),
        }
        if status is MembershipStatus.REVOKED:
            changes["revoked_at"] = dt.datetime.now(dt.UTC)
        result = await self._session.execute(
            update(MembershipRecord)
            .where(MembershipRecord.id == membership_id)
            .values(**changes)
            .returning(MembershipRecord)
        )
        updated = result.scalars().one_or_none()
        return None if updated is None else _to_membership(updated)


class RefreshSessionRepository:
    """Server-side refresh session state, scoped to the bound Organization."""

    def __init__(self, tenant: TenantSession) -> None:
        self._tenant = tenant
        self._session = tenant.session

    async def by_token_hash(self, token_hash: str) -> RefreshSessionRecord | None:
        return (
            (
                await self._session.execute(
                    select(RefreshSessionRecord).where(
                        RefreshSessionRecord.token_hash == token_hash
                    )
                )
            )
            .scalars()
            .one_or_none()
        )

    async def by_previous_token_hash(self, token_hash: str) -> RefreshSessionRecord | None:
        return (
            (
                await self._session.execute(
                    select(RefreshSessionRecord).where(
                        RefreshSessionRecord.previous_token_hash == token_hash
                    )
                )
            )
            .scalars()
            .one_or_none()
        )

    async def by_id(self, session_id: uuid.UUID) -> RefreshSessionRecord | None:
        return await self._session.get(RefreshSessionRecord, session_id)

    async def insert(
        self,
        *,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
        token_hash: str,
        expires_at: dt.datetime,
    ) -> None:
        self._session.add(
            RefreshSessionRecord(
                id=session_id,
                organization_id=self._tenant.organization_id,
                user_id=user_id,
                token_hash=token_hash,
                generation=1,
                expires_at=expires_at,
            )
        )
        await self._session.flush()

    async def rotate(
        self,
        *,
        session_id: uuid.UUID,
        expected_token_hash: str,
        new_token_hash: str,
        now: dt.datetime,
    ) -> bool:
        """Conditional in-place rotation. Returns False when the presenter lost the race
        (stale token) — the caller treats that as replay."""
        result = await self._session.execute(
            update(RefreshSessionRecord)
            .where(
                RefreshSessionRecord.id == session_id,
                RefreshSessionRecord.token_hash == expected_token_hash,
                RefreshSessionRecord.revoked_at.is_(None),
            )
            .values(
                token_hash=new_token_hash,
                previous_token_hash=expected_token_hash,
                generation=RefreshSessionRecord.generation + 1,
                last_used_at=now,
                updated_at=now,
            )
        )
        return cast("CursorResult[Any]", result).rowcount == 1

    async def revoke(self, session_id: uuid.UUID) -> None:
        await self._session.execute(
            update(RefreshSessionRecord)
            .where(RefreshSessionRecord.id == session_id, RefreshSessionRecord.revoked_at.is_(None))
            .values(revoked_at=dt.datetime.now(dt.UTC), updated_at=dt.datetime.now(dt.UTC))
        )


class RoleAssignmentRepository:
    """Organization-scoped role assignments (tenant RLS confines every query)."""

    def __init__(self, tenant: TenantSession) -> None:
        self._session = tenant.session

    async def assign(
        self,
        *,
        assignment_id: uuid.UUID,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        role_id: uuid.UUID,
    ) -> RoleAssignmentRecord:
        now = dt.datetime.now(dt.UTC)
        record = RoleAssignmentRecord(
            id=assignment_id,
            organization_id=organization_id,
            user_id=user_id,
            role_id=role_id,
            status=RoleAssignmentStatus.ACTIVE.value,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise MembershipConflictError(
                "That user already holds that role in this Organization.", cause=exc
            ) from exc
        await self._session.refresh(record)
        return record

    async def set_status(self, assignment_id: uuid.UUID, status: RoleAssignmentStatus) -> None:
        await self._session.execute(
            update(RoleAssignmentRecord)
            .where(RoleAssignmentRecord.id == assignment_id)
            .values(status=status.value, updated_at=dt.datetime.now(dt.UTC))
        )

    async def role_id_by_key(self, role_key: str) -> uuid.UUID | None:
        row = (
            (await self._session.execute(select(RoleRecord).where(RoleRecord.role_key == role_key)))
            .scalars()
            .one_or_none()
        )
        return None if row is None else row.id


class OrganizationScopeProbe:
    """Confirms an Organization exists and is visible in the current tenant scope.

    Used at login: the membership list may be stale or the Organization archived, so
    the token is only minted after the Organization row is re-read through its own
    tenant scope."""

    def __init__(self, tenant: TenantSession) -> None:
        self._tenant = tenant

    async def current_status(self) -> str:
        from nexus_ai.domain.organizations.models import OrganizationRecord

        row = (
            (await self._tenant.session.execute(select(OrganizationRecord).limit(2)))
            .scalars()
            .all()
        )
        if not row:
            raise OrganizationNotFoundError("No Organization is visible in this tenant scope.")
        if len(row) > 1:  # pragma: no cover - RLS makes this impossible
            raise OrganizationNotFoundError("Tenant scope resolved more than one Organization.")
        return str(row[0].status)
