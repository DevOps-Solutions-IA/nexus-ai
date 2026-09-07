"""Authentication domain values (NXS-AUTH-001..008).

ORM rows never cross the service boundary: repositories convert to these immutable
domain views. ``AuthSession`` and ``TokenPair`` are API-boundary values — tokens leave
the server exactly once, in a login/refresh response, and no domain object ever carries
a password back out.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class UserStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class MembershipStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    REVOKED = "REVOKED"


class RoleAssignmentStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class User(BaseModel):
    """Internal domain view of a platform user (global identity plane)."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    email: str
    email_verified: bool
    display_name: str | None
    status: UserStatus
    version: int
    created_at: dt.datetime
    updated_at: dt.datetime


class Membership(BaseModel):
    """Internal domain view of a user-organization membership."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    user_id: UUID
    status: MembershipStatus
    created_at: dt.datetime
    updated_at: dt.datetime
    revoked_at: dt.datetime | None

    @property
    def is_active(self) -> bool:
        return self.status is MembershipStatus.ACTIVE


class MembershipView(BaseModel):
    """Safe public projection of a membership (no cross-organization identifiers)."""

    model_config = ConfigDict(frozen=True)

    organization_id: UUID
    status: MembershipStatus
    created_at: dt.datetime


class Principal(BaseModel):
    """Authenticated caller identity resolved from a verified access token.

    ``organization_id`` is the Organization the token was minted for; it is the ONLY
    source of tenant scope — never a header, never a request body.
    """

    model_config = ConfigDict(frozen=True)

    user_id: UUID
    session_id: UUID
    organization_id: UUID
    token_id: UUID
    issued_at: dt.datetime
    expires_at: dt.datetime


class TokenPair(BaseModel):
    """Credential material handed to the caller exactly once per issuance."""

    model_config = ConfigDict(frozen=True)

    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth2 token type label, not a credential
    expires_in: int


class AuthSession(BaseModel):
    """Result of a successful login or refresh: identity plus fresh tokens."""

    model_config = ConfigDict(frozen=True)

    user_id: UUID
    organization_id: UUID
    session_id: UUID
    tokens: TokenPair


_EmailField = Annotated[str, StringConstraints(min_length=3, max_length=254)]


class LoginRequest(BaseModel):
    """Login payload. ``organization_id`` is a hint, never an authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    email: _EmailField
    password: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    organization_id: UUID | None = None


class RefreshRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    refresh_token: Annotated[str, StringConstraints(min_length=8, max_length=256)]


class LogoutRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    refresh_token: Annotated[str, StringConstraints(min_length=8, max_length=256)]


class SessionView(BaseModel):
    """Safe identity/session projection for ``GET /auth/me``."""

    model_config = ConfigDict(frozen=True)

    user_id: UUID
    email: str
    email_verified: bool
    display_name: str | None
    organization_id: UUID
    session_id: UUID


class RegistrationDraft(BaseModel):
    """Validated input for the internal ``register_user`` seam (P05 provisioner)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    email: _EmailField
    password: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    display_name: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
        | None
    ) = Field(default=None)
