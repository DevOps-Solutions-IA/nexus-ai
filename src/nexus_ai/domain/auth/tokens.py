"""Access and refresh token primitives (NXS-AUTH-004).

Access tokens are short-lived EdDSA JWTs carrying ``sub``, ``org``, ``sid`` and ``jti``
claims with explicit ``iss``/``aud`` and a ``kid`` header. Verification is fail-closed:
a strict single-algorithm allowlist, required exp/iat/nbf, bounded clock skew, a
required key identifier and required typ all reject malformed or confused tokens.

Refresh tokens are opaque 256-bit random strings prefixed with the base32-encoded
organization id. The prefix is NOT trusted on its own — it only selects the tenant
scope in which the server-side session row must exist; a forged prefix finds no row and
fails identically to a bad token. The stored form is always the SHA-256 of the full
token, never the token itself.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from typing import Any, Final

import jwt
from jwt.exceptions import InvalidTokenError as PyJwtInvalidTokenError

from nexus_ai.core.errors import TokenValidationError
from nexus_ai.domain.auth.keys import KeyNotFoundError, SigningKeyProvider

REFRESH_PREFIX_SEPARATOR: Final = "."
TOKEN_TYPE_ACCESS: Final = "access"  # noqa: S105 - JWT typ header value, not a credential


@dataclass(frozen=True, slots=True)
class AccessClaims:
    subject: uuid.UUID
    organization_id: uuid.UUID
    session_id: uuid.UUID
    jti: uuid.UUID
    issued_at: dt.datetime
    expires_at: dt.datetime

    def as_audit_fields(self) -> dict[str, str]:
        return {
            "user_id": str(self.subject),
            "organization_id": str(self.organization_id),
            "session_id": str(self.session_id),
            "token_id": str(self.jti),
        }


@dataclass(frozen=True, slots=True)
class RefreshTokenParts:
    """Parsed shape of a refresh token: the tenant scope prefix and its hash."""

    organization_id: uuid.UUID
    token_hash: str


def new_refresh_token(organization_id: uuid.UUID) -> str:
    """Opaque refresh token with an org-scope prefix (never stored verbatim)."""
    prefix = base64.b32hexencode(organization_id.bytes).decode("ascii").rstrip("=").lower()
    return f"{prefix}{REFRESH_PREFIX_SEPARATOR}{secrets.token_urlsafe(48)}"


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def parse_refresh_token(token: str) -> RefreshTokenParts:
    """Split prefix and hash a refresh token; fail closed on any malformed shape."""
    if REFRESH_PREFIX_SEPARATOR not in token or len(token) > 256:
        raise TokenValidationError("Invalid token.")
    prefix, _rest = token.split(REFRESH_PREFIX_SEPARATOR, 1)
    if not prefix or not _rest:
        raise TokenValidationError("Invalid token.")
    try:
        padded = prefix.upper() + "=" * (-len(prefix) % 8)
        organization_id = uuid.UUID(bytes=base64.b32hexdecode(padded))
    except (ValueError, binascii.Error) as exc:
        raise TokenValidationError("Invalid token.") from exc
    return RefreshTokenParts(organization_id=organization_id, token_hash=hash_refresh_token(token))


class TokenService:
    """Issues and validates access tokens and refresh token shapes (NXS-AUTH-004)."""

    def __init__(
        self,
        keys: SigningKeyProvider,
        *,
        issuer: str,
        audience: str,
        access_token_ttl_seconds: int,
        clock_skew_seconds: int,
    ) -> None:
        self._keys = keys
        self._issuer = issuer
        self._audience = audience
        self._ttl = access_token_ttl_seconds
        self._skew = clock_skew_seconds

    @property
    def issuer(self) -> str:
        return self._issuer

    @property
    def audience(self) -> str:
        return self._audience

    def issue_access_token(
        self,
        *,
        subject: uuid.UUID,
        organization_id: uuid.UUID,
        session_id: uuid.UUID,
        now: dt.datetime,
    ) -> str:
        signing = self._keys.signing_key()
        issued = now.astimezone(dt.UTC)
        payload: dict[str, Any] = {
            "iss": self._issuer,
            "aud": self._audience,
            "sub": str(subject),
            "org": str(organization_id),
            "sid": str(session_id),
            "jti": str(uuid.uuid7()),
            "iat": issued,
            "nbf": issued,
            "exp": issued + dt.timedelta(seconds=self._ttl),
        }
        return jwt.encode(
            payload,
            signing.private_key,
            algorithm="EdDSA",
            headers={"kid": signing.kid, "typ": TOKEN_TYPE_ACCESS},
        )

    def verify_access_token(self, token: str, *, now: dt.datetime | None = None) -> AccessClaims:
        if now is None:
            now = dt.datetime.now(dt.UTC)
        if not token or len(token) > 16_384:
            raise TokenValidationError("Invalid token.")
        try:
            unverified = jwt.get_unverified_header(token)
        except PyJwtInvalidTokenError as exc:
            raise TokenValidationError("Invalid token.") from exc
        kid = unverified.get("kid")
        if not isinstance(kid, str) or not kid:
            raise TokenValidationError("Invalid token.")
        if unverified.get("typ") != TOKEN_TYPE_ACCESS:
            # Explicit token-type binding: an access token may only ever be an access
            # token, whatever the library's per-version typ semantics happen to be.
            raise TokenValidationError("Invalid token.")
        try:
            verification_key = self._keys.verification_key(kid)
        except KeyNotFoundError as exc:
            raise TokenValidationError("Invalid token.") from exc
        try:
            payload = jwt.decode(
                token,
                verification_key,
                algorithms=["EdDSA"],
                issuer=self._issuer,
                audience=self._audience,
                options={
                    "require": ["exp", "iat", "nbf", "sub", "org", "sid", "jti"],
                    "verify_iss": True,
                    "verify_aud": True,
                    # Time claims are validated explicitly below against the injected
                    # clock with the bounded skew policy — PyJWT offers no clock
                    # injection, and an explicit policy is the requirement anyway.
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                    "verify_signature": True,
                },
            )
        except PyJwtInvalidTokenError as exc:
            raise TokenValidationError("Invalid token.") from exc
        try:
            issued_at = self._require_time_claim(payload, "iat", now)
            expires_at = self._require_time_claim(payload, "exp", now)
            self._require_time_claim(payload, "nbf", now)
            subject = uuid.UUID(str(payload["sub"]))
            organization_id = uuid.UUID(str(payload["org"]))
            session_id = uuid.UUID(str(payload["sid"]))
            jti = uuid.UUID(str(payload["jti"]))
        except (KeyError, ValueError, AttributeError, TypeError) as exc:
            raise TokenValidationError("Invalid token.") from exc
        return AccessClaims(
            subject=subject,
            organization_id=organization_id,
            session_id=session_id,
            jti=jti,
            issued_at=issued_at,
            expires_at=expires_at,
        )

    def _require_time_claim(
        self, payload: dict[str, Any], name: str, now: dt.datetime
    ) -> dt.datetime:
        """Bounded clock-skew time validation (NXS-AUTH-004).

        ``exp``: now must not exceed exp + skew. ``nbf``/``iat``: now must not
        precede the bound minus skew. Fail closed on any non-numeric value.
        """
        raw = payload.get(name)
        if not isinstance(raw, (int, float)):
            raise TokenValidationError("Invalid token.")
        bound = dt.datetime.fromtimestamp(float(raw), dt.UTC)
        skew = dt.timedelta(seconds=self._skew)
        if name == "exp" and now.astimezone(dt.UTC) > bound + skew:
            raise TokenValidationError("Invalid token.")
        if name in {"nbf", "iat"} and now.astimezone(dt.UTC) < bound - skew:
            raise TokenValidationError("Invalid token.")
        return bound
