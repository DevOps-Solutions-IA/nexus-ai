"""Authentication domain service (NXS-AUTH-001..009).

The service owns every authentication decision:

* ``register_user`` — the internal identity-creation seam (P05 provisioner and tests);
  there is deliberately no public registration endpoint.
* ``login`` — rate-limit gate, constant-work credential verification with a dummy
  verify for unknown emails, active-user check, membership resolution through the
  principal self-visibility policy, Organization operational check through the tenant
  scope, refresh-session creation and token minting. Tenant authority NEVER comes from
  the request body: a supplied ``organization_id`` is only honored when it appears in
  the caller's own ACTIVE membership rows.
* ``refresh`` — rotating server-side session update with replay revocation.
* ``logout`` — idempotent revocation with no oracle.
* ``session_view`` / ``list_memberships`` — authenticated identity projections.

Every failure mode that could distinguish one identity from another raises the same
``AuthenticationFailedError`` before authentication, and structured security events are
emitted for the observability seam (P22 owns the full audit ledger).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from uuid import UUID

from nexus_ai.core.config import Settings
from nexus_ai.core.errors import (
    AuthenticationFailedError,
    MembershipInactiveError,
    MembershipRequiredError,
    MultipleOrganizationsError,
    OrganizationInactiveError,
    SessionRevokedError,
    TokenValidationError,
    UserInactiveError,
    ValidationFailedError,
)
from nexus_ai.core.logging import get_logger
from nexus_ai.domain.auth.entities import (
    AuthSession,
    LoginRequest,
    Membership,
    MembershipView,
    Principal,
    RegistrationDraft,
    SessionView,
    TokenPair,
    User,
    UserStatus,
)
from nexus_ai.domain.auth.identity import normalize_email, validate_password
from nexus_ai.domain.auth.passwords import (
    CREDENTIAL_ALGORITHM,
    CREDENTIAL_VERSION,
    PasswordHasherService,
)
from nexus_ai.domain.auth.ratelimit import RateLimitGate, login_rate_key
from nexus_ai.domain.auth.repository import (
    CredentialRepository,
    MembershipRepository,
    OrganizationScopeProbe,
    RefreshSessionRepository,
    UserRepository,
)
from nexus_ai.domain.auth.tokens import (
    TokenService,
    hash_refresh_token,
    new_refresh_token,
    parse_refresh_token,
)
from nexus_ai.domain.organizations.status import OrganizationStatus, is_operational
from nexus_ai.infrastructure.database import Database


class AuthService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        tokens: TokenService,
        hasher: PasswordHasherService,
        rate_gate: RateLimitGate,
        *,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._db = database
        self._tokens = tokens
        self._hasher = hasher
        self._rate_gate = rate_gate
        self._now = now or (lambda: dt.datetime.now(dt.UTC))
        self._logger = get_logger("nexus_ai.auth")

    # -- identity creation -----------------------------------------------------------

    async def register_user(self, draft: RegistrationDraft, *, verify_email: bool = False) -> User:
        email = normalize_email(draft.email)
        password = validate_password(
            draft.password,
            min_length=self._settings.auth.min_password_length,
            max_length=self._settings.auth.max_password_length,
        )
        user_id = uuid.uuid7()
        async with self._db.transaction() as session:
            user = await UserRepository(session).insert(
                user_id=user_id,
                email=email,
                display_name=draft.display_name,
                email_verified=verify_email,
            )
            await CredentialRepository(session).insert(
                user_id=user_id,
                credential_version=CREDENTIAL_VERSION,
                algorithm=CREDENTIAL_ALGORITHM,
                parameters=self._hasher.parameters,
                password_hash=self._hasher.hash(password),
            )
        await self._logger.ainfo("auth_user_registered", user_id=str(user_id))
        return user

    # -- login -----------------------------------------------------------------------

    async def login(self, request: LoginRequest, *, client_ip: str) -> AuthSession:
        email = normalize_email(request.email)
        try:
            validate_password(
                request.password,
                min_length=self._settings.auth.min_password_length,
                max_length=self._settings.auth.max_password_length,
            )
        except ValidationFailedError:
            # Pathological input is rejected before any hashing work happens.
            raise AuthenticationFailedError("Invalid email or password.") from None
        rate_key = login_rate_key(email, client_ip)
        await self._rate_gate.check(rate_key)

        user_id = await self._authenticate(email, request.password)
        try:
            memberships = await self._active_memberships(user_id)
            organization_id = self._select_organization(memberships, request.organization_id)
            session = await self._establish_session(user_id, organization_id)
        except AuthenticationFailedError, MembershipInactiveError:
            await self._rate_gate.record_failure(rate_key)
            raise
        await self._rate_gate.reset(rate_key)
        await self._logger.ainfo(
            "auth_login_success",
            user_id=str(user_id),
            organization_id=str(organization_id),
            session_id=str(session.session_id),
        )
        return session

    async def _authenticate(self, email: str, password: str) -> UUID:
        async with self._db.transaction() as session:
            user = await UserRepository(session).by_email(email)
            if user is None:
                self._hasher.verify_dummy(password)
                raise AuthenticationFailedError("Invalid email or password.")
            credential = await CredentialRepository(session).get_latest(user.id)
            verified = credential is not None and self._hasher.verify(
                credential.password_hash, password
            )
            if not verified or user.status is not UserStatus.ACTIVE:
                raise AuthenticationFailedError("Invalid email or password.")
            return user.id

    async def _active_memberships(self, user_id: UUID) -> list[Membership]:
        async with self._db.principal_session(user_id) as session:
            memberships = await MembershipRepository(session).for_user(user_id)
        return [m for m in memberships if m.is_active]

    @staticmethod
    def _select_organization(memberships: list[Membership], requested: UUID | None) -> UUID:
        if not memberships:
            raise MembershipRequiredError("The user has no active Organization membership.")
        if requested is not None:
            if all(m.organization_id != requested for m in memberships):
                # No oracle: same generic failure whether the hint is a foreign org,
                # a suspended membership or a nonexistent id.
                raise AuthenticationFailedError("Invalid email or password.")
            return requested
        if len(memberships) == 1:
            return memberships[0].organization_id
        raise MultipleOrganizationsError(
            "The user belongs to several Organizations; select one explicitly.",
            extensions={
                "organizations": [
                    {
                        "organization_id": str(m.organization_id),
                        "membership_status": m.status.value,
                    }
                    for m in memberships
                ]
            },
        )

    async def _establish_session(self, user_id: UUID, organization_id: UUID) -> AuthSession:
        session_id = uuid.uuid7()
        refresh_token = new_refresh_token(organization_id)
        now = self._now()
        async with self._db.tenant_transaction(organization_id) as tenant:
            status = await OrganizationScopeProbe(tenant).current_status()
            if not is_operational(OrganizationStatus(status)):
                raise OrganizationInactiveError(
                    "The Organization is not accepting sign-ins.",
                    extensions={"organization_status": status},
                )
            membership = await MembershipRepository(tenant).for_user_in_organization(
                user_id, organization_id
            )
            if membership is None or not membership.is_active:
                raise MembershipInactiveError(
                    "The user's membership in this Organization is not active."
                )
            await RefreshSessionRepository(tenant).insert(
                session_id=session_id,
                user_id=user_id,
                token_hash=hash_refresh_token(refresh_token),
                expires_at=now
                + dt.timedelta(seconds=self._settings.auth.refresh_token_ttl_seconds),
            )
        access_token = self._tokens.issue_access_token(
            subject=user_id, organization_id=organization_id, session_id=session_id, now=now
        )
        return AuthSession(
            user_id=user_id,
            organization_id=organization_id,
            session_id=session_id,
            tokens=TokenPair(
                access_token=access_token,
                refresh_token=refresh_token,
                expires_in=self._settings.auth.access_token_ttl_seconds,
            ),
        )

    # -- refresh ---------------------------------------------------------------------

    async def refresh(self, raw_token: str) -> AuthSession:
        try:
            parts = parse_refresh_token(raw_token)
        except TokenValidationError:
            raise AuthenticationFailedError("The session could not be refreshed.") from None
        now = self._now()
        async with self._db.tenant_transaction(parts.organization_id) as tenant:
            sessions = RefreshSessionRepository(tenant)
            row = await sessions.by_token_hash(parts.token_hash)
            if row is None or row.revoked_at is not None or row.expires_at <= now:
                raise AuthenticationFailedError("The session could not be refreshed.")
            user = await UserRepository(tenant.session).by_id(row.user_id)
            if user is None or user.status is not UserStatus.ACTIVE:
                await sessions.revoke(row.id)
                raise AuthenticationFailedError("The session could not be refreshed.")
            membership = await MembershipRepository(tenant).for_user_in_organization(
                row.user_id, parts.organization_id
            )
            if membership is None or not membership.is_active:
                raise AuthenticationFailedError("The session could not be refreshed.")
            next_refresh_token = new_refresh_token(parts.organization_id)
            rotated = await sessions.rotate(
                session_id=row.id,
                expected_token_hash=parts.token_hash,
                new_token_hash=hash_refresh_token(next_refresh_token),
                now=now,
            )
            if not rotated:
                # The presented token lost a rotation race: reuse. Revoke the family.
                await sessions.revoke(row.id)
                await self._logger.awarning("auth_refresh_reuse_detected", session_id=str(row.id))
                raise AuthenticationFailedError("The session could not be refreshed.")
        access_token = self._tokens.issue_access_token(
            subject=row.user_id,
            organization_id=parts.organization_id,
            session_id=row.id,
            now=now,
        )
        await self._logger.ainfo("auth_refresh_rotated", session_id=str(row.id))
        return AuthSession(
            user_id=row.user_id,
            organization_id=parts.organization_id,
            session_id=row.id,
            tokens=TokenPair(
                access_token=access_token,
                refresh_token=next_refresh_token,
                expires_in=self._settings.auth.access_token_ttl_seconds,
            ),
        )

    # -- logout / revocation ---------------------------------------------------------

    async def logout(self, raw_token: str) -> None:
        """Idempotent revocation. Unknown or malformed tokens revoke nothing and reveal
        nothing — the response is identical in every case."""
        try:
            parts = parse_refresh_token(raw_token)
        except TokenValidationError:
            return
        async with self._db.tenant_transaction(parts.organization_id) as tenant:
            sessions = RefreshSessionRepository(tenant)
            row = await sessions.by_token_hash(parts.token_hash)
            if row is not None and row.revoked_at is None:
                await sessions.revoke(row.id)
                await self._logger.ainfo("auth_session_revoked", session_id=str(row.id))

    # -- authenticated projections ---------------------------------------------------

    async def session_view(self, principal: Principal) -> SessionView:
        async with self._db.tenant_transaction(principal.organization_id) as tenant:
            row = await RefreshSessionRepository(tenant).by_id(principal.session_id)
            if row is None or row.revoked_at is not None or row.expires_at <= self._now():
                raise SessionRevokedError("The session is no longer active.")
            user = await UserRepository(tenant.session).by_id(principal.user_id)
            if user is None or user.status is not UserStatus.ACTIVE:
                raise UserInactiveError("The user is not active.")
            return SessionView(
                user_id=user.id,
                email=user.email,
                email_verified=user.email_verified,
                display_name=user.display_name,
                organization_id=principal.organization_id,
                session_id=principal.session_id,
            )

    async def list_memberships(self, principal: Principal) -> list[MembershipView]:
        async with self._db.principal_session(principal.user_id) as session:
            memberships = await MembershipRepository(session).for_user(principal.user_id)
        return [
            MembershipView(
                organization_id=m.organization_id, status=m.status, created_at=m.created_at
            )
            for m in memberships
        ]
