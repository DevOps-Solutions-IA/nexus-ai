"""Adversarial token test matrix (NXS-AUTH-004, NXS-AUTH-005).

Every entry in the TOKEN ATTACKS matrix from the P03 branch contract: expired,
future-nbf, malformed, wrong issuer/audience/signature/key, altered payload, unknown
kid, disallowed algorithms, alg=none, typ confusion and rotation behavior.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nexus_ai.core.errors import TokenValidationError
from nexus_ai.domain.auth.keys import (
    LocalEd25519KeyProvider,
    derive_kid,
)
from nexus_ai.domain.auth.tokens import (
    REFRESH_PREFIX_SEPARATOR,
    TokenService,
    hash_refresh_token,
    new_refresh_token,
    parse_refresh_token,
)

ISSUER = "nexus-ai"
AUDIENCE = "nexus-ai-backend"
NOW = dt.datetime(2026, 9, 7, 12, 0, 0, tzinfo=dt.UTC)


@pytest.fixture
def keys() -> LocalEd25519KeyProvider:
    return LocalEd25519KeyProvider(Ed25519PrivateKey.generate())


@pytest.fixture
def tokens(keys: LocalEd25519KeyProvider) -> TokenService:
    return TokenService(
        keys,
        issuer=ISSUER,
        audience=AUDIENCE,
        access_token_ttl_seconds=900,
        clock_skew_seconds=30,
    )


def _issue(
    tokens: TokenService,
    *,
    now: dt.datetime = NOW,
    subject: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
) -> str:
    return tokens.issue_access_token(
        subject=subject or uuid.uuid7(),
        organization_id=organization_id or uuid.uuid7(),
        session_id=session_id or uuid.uuid7(),
        now=now,
    )


class TestTokenAttackMatrix:
    def test_valid_token_verifies(self, tokens: TokenService) -> None:
        token = _issue(tokens)
        claims = tokens.verify_access_token(token, now=NOW + dt.timedelta(seconds=60))
        assert claims.organization_id is not None

    def test_expired_token_rejected(self, tokens: TokenService) -> None:
        token = _issue(tokens)
        with pytest.raises(TokenValidationError):
            tokens.verify_access_token(token, now=NOW + dt.timedelta(seconds=931))

    def test_expiry_within_clock_skew_accepted(self, tokens: TokenService) -> None:
        token = _issue(tokens)
        tokens.verify_access_token(token, now=NOW + dt.timedelta(seconds=929))  # 29s skew

    def test_future_nbf_rejected(self, tokens: TokenService) -> None:
        token = _issue(tokens, now=NOW)
        with pytest.raises(TokenValidationError):
            tokens.verify_access_token(token, now=NOW - dt.timedelta(seconds=60))

    def test_iat_far_future_rejected(self, tokens: TokenService) -> None:
        token = _issue(tokens, now=NOW)
        with pytest.raises(TokenValidationError):
            tokens.verify_access_token(token, now=NOW - dt.timedelta(seconds=3600))

    @pytest.mark.parametrize("malformed", ["", "garbage", "a.b", "a.b.c.d", "....", "x" * 20_000])
    def test_malformed_tokens_fail_closed(self, tokens: TokenService, malformed: str) -> None:
        with pytest.raises(TokenValidationError):
            tokens.verify_access_token(malformed, now=NOW)

    def test_wrong_issuer_rejected(
        self, keys: LocalEd25519KeyProvider, tokens: TokenService
    ) -> None:
        signing = keys.signing_key()
        forged = jwt.encode(
            {
                "iss": "evil-issuer",
                "aud": AUDIENCE,
                "sub": str(uuid.uuid7()),
                "org": str(uuid.uuid7()),
                "sid": str(uuid.uuid7()),
                "jti": str(uuid.uuid7()),
                "iat": NOW,
                "nbf": NOW,
                "exp": NOW + dt.timedelta(seconds=900),
            },
            signing.private_key,
            algorithm="EdDSA",
            headers={"kid": signing.kid, "typ": "access"},
        )
        with pytest.raises(TokenValidationError):
            tokens.verify_access_token(forged, now=NOW)

    def test_wrong_audience_rejected(
        self, keys: LocalEd25519KeyProvider, tokens: TokenService
    ) -> None:
        signing = keys.signing_key()
        forged = jwt.encode(
            {
                "iss": ISSUER,
                "aud": "other-service",
                "sub": str(uuid.uuid7()),
                "org": str(uuid.uuid7()),
                "sid": str(uuid.uuid7()),
                "jti": str(uuid.uuid7()),
                "iat": NOW,
                "nbf": NOW,
                "exp": NOW + dt.timedelta(seconds=900),
            },
            signing.private_key,
            algorithm="EdDSA",
            headers={"kid": signing.kid, "typ": "access"},
        )
        with pytest.raises(TokenValidationError):
            tokens.verify_access_token(forged, now=NOW)

    def test_wrong_signature_rejected(
        self, keys: LocalEd25519KeyProvider, tokens: TokenService
    ) -> None:
        attacker = Ed25519PrivateKey.generate()
        forged = jwt.encode(
            {
                "iss": ISSUER,
                "aud": AUDIENCE,
                "sub": str(uuid.uuid7()),
                "org": str(uuid.uuid7()),
                "sid": str(uuid.uuid7()),
                "jti": str(uuid.uuid7()),
                "iat": NOW,
                "nbf": NOW,
                "exp": NOW + dt.timedelta(seconds=900),
            },
            attacker,
            algorithm="EdDSA",
            headers={"kid": keys.signing_key().kid, "typ": "access"},
        )
        with pytest.raises(TokenValidationError):
            tokens.verify_access_token(forged, now=NOW)

    def test_altered_payload_rejected(self, tokens: TokenService) -> None:
        token = _issue(tokens)
        header, payload, signature = token.split(".")
        decoded = jwt.utils.base64url_decode(payload)
        claims = json.loads(decoded)
        claims["org"] = str(uuid.uuid7())  # tamper with tenant scope
        tampered_payload = jwt.utils.base64url_encode(json.dumps(claims).encode("utf-8"))
        with pytest.raises(TokenValidationError):
            tokens.verify_access_token(f"{header}.{tampered_payload}.{signature}", now=NOW)

    def test_unknown_kid_rejected(self, tokens: TokenService) -> None:
        token = _issue(tokens)
        _header, payload, signature = token.split(".")
        forged_header = jwt.utils.base64url_encode(
            b'{"alg":"EdDSA","kid":"nonexistent","typ":"access"}'
        )
        with pytest.raises(TokenValidationError):
            tokens.verify_access_token(f"{forged_header}.{payload}.{signature}", now=NOW)

    def test_missing_kid_rejected(
        self, keys: LocalEd25519KeyProvider, tokens: TokenService
    ) -> None:
        signing = keys.signing_key()
        forged = jwt.encode(
            {
                "iss": ISSUER,
                "aud": AUDIENCE,
                "sub": str(uuid.uuid7()),
                "org": str(uuid.uuid7()),
                "sid": str(uuid.uuid7()),
                "jti": str(uuid.uuid7()),
                "iat": NOW,
                "nbf": NOW,
                "exp": NOW + dt.timedelta(seconds=900),
            },
            signing.private_key,
            algorithm="EdDSA",
            headers={"typ": "access"},
        )
        with pytest.raises(TokenValidationError):
            tokens.verify_access_token(forged, now=NOW)

    def test_alg_none_rejected(self, tokens: TokenService) -> None:
        payload = jwt.utils.base64url_encode(
            json.dumps(
                {
                    "iss": ISSUER,
                    "aud": AUDIENCE,
                    "sub": str(uuid.uuid7()),
                    "org": str(uuid.uuid7()),
                    "sid": str(uuid.uuid7()),
                    "jti": str(uuid.uuid7()),
                    "iat": int(NOW.timestamp()),
                    "nbf": int(NOW.timestamp()),
                    "exp": int((NOW + dt.timedelta(seconds=900)).timestamp()),
                }
            ).encode("utf-8")
        )
        header = jwt.utils.base64url_encode(b'{"alg":"none","kid":"whatever","typ":"access"}')
        with pytest.raises(TokenValidationError):
            tokens.verify_access_token(f"{header}.{payload}.", now=NOW)

    def test_hmac_confusion_with_public_key_as_secret_rejected(
        self, keys: LocalEd25519KeyProvider, tokens: TokenService
    ) -> None:
        # The classic RS256→HS256 confusion: sign with the PUBLIC key bytes as an HMAC
        # secret. The strict EdDSA-only allowlist must refuse it.
        from cryptography.hazmat.primitives import serialization

        signing = keys.signing_key()
        public_bytes = signing.public_key.public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
        forged = jwt.encode(
            {
                "iss": ISSUER,
                "aud": AUDIENCE,
                "sub": str(uuid.uuid7()),
                "org": str(uuid.uuid7()),
                "sid": str(uuid.uuid7()),
                "jti": str(uuid.uuid7()),
                "iat": NOW,
                "nbf": NOW,
                "exp": NOW + dt.timedelta(seconds=900),
            },
            public_bytes,
            algorithm="HS256",
            headers={"kid": signing.kid, "typ": "access"},
        )
        with pytest.raises(TokenValidationError):
            tokens.verify_access_token(forged, now=NOW)

    def test_disallowed_algorithm_rejected(
        self, keys: LocalEd25519KeyProvider, tokens: TokenService
    ) -> None:
        signing = keys.signing_key()
        forged = jwt.encode(
            {
                "iss": ISSUER,
                "aud": AUDIENCE,
                "sub": str(uuid.uuid7()),
                "org": str(uuid.uuid7()),
                "sid": str(uuid.uuid7()),
                "jti": str(uuid.uuid7()),
                "iat": NOW,
                "nbf": NOW,
                "exp": NOW + dt.timedelta(seconds=900),
            },
            "shared-secret-that-is-long-enough",
            algorithm="HS256",
            headers={"kid": signing.kid, "typ": "access"},
        )
        with pytest.raises(TokenValidationError):
            tokens.verify_access_token(forged, now=NOW)

    def test_wrong_typ_rejected(self, keys: LocalEd25519KeyProvider, tokens: TokenService) -> None:
        signing = keys.signing_key()
        forged = jwt.encode(
            {
                "iss": ISSUER,
                "aud": AUDIENCE,
                "sub": str(uuid.uuid7()),
                "org": str(uuid.uuid7()),
                "sid": str(uuid.uuid7()),
                "jti": str(uuid.uuid7()),
                "iat": NOW,
                "nbf": NOW,
                "exp": NOW + dt.timedelta(seconds=900),
            },
            signing.private_key,
            algorithm="EdDSA",
            headers={"kid": signing.kid, "typ": "refresh"},
        )
        with pytest.raises(TokenValidationError):
            tokens.verify_access_token(forged, now=NOW)

    @pytest.mark.parametrize("missing", ["sub", "org", "sid", "jti", "exp", "iat", "nbf"])
    def test_missing_required_claims_rejected(
        self, keys: LocalEd25519KeyProvider, tokens: TokenService, missing: str
    ) -> None:
        signing = keys.signing_key()
        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": str(uuid.uuid7()),
            "org": str(uuid.uuid7()),
            "sid": str(uuid.uuid7()),
            "jti": str(uuid.uuid7()),
            "iat": NOW,
            "nbf": NOW,
            "exp": NOW + dt.timedelta(seconds=900),
        }
        del claims[missing]
        forged = jwt.encode(
            claims,
            signing.private_key,
            algorithm="EdDSA",
            headers={"kid": signing.kid, "typ": "access"},
        )
        with pytest.raises(TokenValidationError):
            tokens.verify_access_token(forged, now=NOW)

    def test_non_uuid_claim_values_rejected(
        self, keys: LocalEd25519KeyProvider, tokens: TokenService
    ) -> None:
        signing = keys.signing_key()
        forged = jwt.encode(
            {
                "iss": ISSUER,
                "aud": AUDIENCE,
                "sub": "not-a-uuid",
                "org": "00",
                "sid": "x",
                "jti": ";drop table",
                "iat": NOW,
                "nbf": NOW,
                "exp": NOW + dt.timedelta(seconds=900),
            },
            signing.private_key,
            algorithm="EdDSA",
            headers={"kid": signing.kid, "typ": "access"},
        )
        with pytest.raises(TokenValidationError):
            tokens.verify_access_token(forged, now=NOW)

    def test_wrong_key_provider_rejected(self, tokens: TokenService) -> None:
        other = TokenService(
            LocalEd25519KeyProvider(Ed25519PrivateKey.generate()),
            issuer=ISSUER,
            audience=AUDIENCE,
            access_token_ttl_seconds=900,
            clock_skew_seconds=30,
        )
        token = _issue(other)
        with pytest.raises(TokenValidationError):
            tokens.verify_access_token(token, now=NOW)


class TestKeyRotation:
    def test_rotated_key_still_verifies_old_tokens(self) -> None:
        old_key = Ed25519PrivateKey.generate()
        old_public = old_key.public_key()
        provider = LocalEd25519KeyProvider(
            old_key,
            verification={derive_kid(_raw(old_public)): old_public},
        )
        old_kid = provider.all_kids()
        service = TokenService(
            provider,
            issuer=ISSUER,
            audience=AUDIENCE,
            access_token_ttl_seconds=900,
            clock_skew_seconds=30,
        )
        token = service.issue_access_token(
            subject=uuid.uuid7(), organization_id=uuid.uuid7(), session_id=uuid.uuid7(), now=NOW
        )
        # Rotate: brand-new primary, old key kept for verification.
        rotated = LocalEd25519KeyProvider(
            Ed25519PrivateKey.generate(),
            verification={derive_kid(_raw(old_public)): old_public},
        )
        rotated_service = TokenService(
            rotated,
            issuer=ISSUER,
            audience=AUDIENCE,
            access_token_ttl_seconds=900,
            clock_skew_seconds=30,
        )
        claims = rotated_service.verify_access_token(token, now=NOW + dt.timedelta(seconds=1))
        assert claims.organization_id is not None
        # And the new key signs new tokens under its own kid.
        fresh = rotated_service.issue_access_token(
            subject=uuid.uuid7(), organization_id=uuid.uuid7(), session_id=uuid.uuid7(), now=NOW
        )
        assert jwt.get_unverified_header(fresh)["kid"] != next(iter(old_kid))

    def test_removed_key_rejects_old_tokens(self) -> None:
        old_key = Ed25519PrivateKey.generate()
        service = TokenService(
            LocalEd25519KeyProvider(old_key),
            issuer=ISSUER,
            audience=AUDIENCE,
            access_token_ttl_seconds=900,
            clock_skew_seconds=30,
        )
        token = service.issue_access_token(
            subject=uuid.uuid7(), organization_id=uuid.uuid7(), session_id=uuid.uuid7(), now=NOW
        )
        replacement = TokenService(
            LocalEd25519KeyProvider(Ed25519PrivateKey.generate()),
            issuer=ISSUER,
            audience=AUDIENCE,
            access_token_ttl_seconds=900,
            clock_skew_seconds=30,
        )
        with pytest.raises(TokenValidationError):
            replacement.verify_access_token(token, now=NOW + dt.timedelta(seconds=1))


def _raw(public_key: object) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return public_key.public_bytes(  # type: ignore[union-attr]
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )


class TestRefreshTokenShape:
    def test_roundtrip_parse(self) -> None:
        org = uuid.uuid7()
        token = new_refresh_token(org)
        parts = parse_refresh_token(token)
        assert parts.organization_id == org
        assert parts.token_hash == hash_refresh_token(token)

    def test_tokens_are_opaque_and_high_entropy(self) -> None:
        org = uuid.uuid7()
        first, second = new_refresh_token(org), new_refresh_token(org)
        assert first != second
        assert len(first) > 64
        assert REFRESH_PREFIX_SEPARATOR in first

    @pytest.mark.parametrize("bad", ["", "noprefix", "....", "x" * 300, "ZZZZ.abc"])
    def test_malformed_refresh_tokens_fail_closed(self, bad: str) -> None:
        with pytest.raises(TokenValidationError):
            parse_refresh_token(bad)

    def test_forged_org_prefix_parses_but_never_authenticates(self) -> None:
        # A forged prefix produces a well-formed parse — authority lives in the
        # server-side session row, which the forged prefix cannot reach (integration
        # tests prove the end-to-end rejection).
        token = new_refresh_token(uuid.uuid7())
        prefix, random_part = token.split(REFRESH_PREFIX_SEPARATOR, 1)
        forged = f"{prefix}{REFRESH_PREFIX_SEPARATOR}{'A' * len(random_part)}"
        parts = parse_refresh_token(forged)
        assert parts.token_hash != hash_refresh_token(token)
