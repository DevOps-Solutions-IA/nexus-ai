"""Canonical server-side security-state validation for authenticated principals.

A cryptographically valid access token proves WHO asked and WHAT Organization the
token was minted for — it does NOT prove the authorization is still in force.
``PrincipalStateValidator`` is the single boundary that re-checks live security state
before tenant authority is granted: the refresh session must exist and be neither
revoked nor expired, the user must be ACTIVE, the membership in the token's
Organization must be ACTIVE, and the Organization must be operational.

Every consumer of tenant scope goes through this boundary exactly once:

* ``BearerTokenTenantContextResolver`` — before a ``TenantContext`` is produced;
* ``AuthService.session_view`` / ``list_memberships`` — authenticated projections.

The check runs inside a tenant transaction, so every lookup is RLS-confined to the
token's Organization: a foreign or stale row can never satisfy it, and a mismatch
fails closed with the canonical error contract (401/403 — never 200).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from nexus_ai.core.errors import (
    MembershipInactiveError,
    OrganizationInactiveError,
    SessionRevokedError,
    UserInactiveError,
)
from nexus_ai.domain.auth.entities import User, UserStatus
from nexus_ai.domain.auth.repository import (
    MembershipRepository,
    OrganizationScopeProbe,
    RefreshSessionRepository,
    UserRepository,
)
from nexus_ai.domain.organizations.status import OrganizationStatus, is_operational
from nexus_ai.infrastructure.database import Database


class PrincipalStateGate(Protocol):
    """The dependency contract the tenant resolver binds to (injectable in tests)."""

    async def require_valid(
        self, *, user_id: UUID, session_id: UUID, organization_id: UUID
    ) -> User: ...


class PrincipalStateValidator:
    """Live-state validation boundary (independent-audit Finding 1)."""

    def __init__(self, database: Database, *, now: Callable[[], dt.datetime] | None = None) -> None:
        self._db = database
        self._now = now or (lambda: dt.datetime.now(dt.UTC))

    async def require_valid(
        self, *, user_id: UUID, session_id: UUID, organization_id: UUID
    ) -> User:
        now = self._now()
        async with self._db.tenant_transaction(organization_id) as tenant:
            session = await RefreshSessionRepository(tenant).by_id(session_id)
            if session is None or session.revoked_at is not None or session.expires_at <= now:
                # RLS already hides foreign-org sessions; a missing row here means the
                # session does not exist in THIS Organization's scope — fail closed.
                raise SessionRevokedError("The session is no longer active.")
            user = await UserRepository(tenant.session).by_id(user_id)
            if user is None or user.status is not UserStatus.ACTIVE:
                raise UserInactiveError("The user is not active.")
            membership = await MembershipRepository(tenant).for_user_in_organization(
                user_id, organization_id
            )
            if membership is None or not membership.is_active:
                raise MembershipInactiveError(
                    "The user's membership in this Organization is not active."
                )
            organization_status = await OrganizationScopeProbe(tenant).current_status()
            if not is_operational(OrganizationStatus(organization_status)):
                raise OrganizationInactiveError(
                    "The Organization is not accepting normal operations.",
                    extensions={"organization_status": organization_status},
                )
            return user
