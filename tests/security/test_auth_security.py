"""Authentication security matrix (NXS-AUTH-004/007).

The authenticated tenant resolver's confused-deputy protections and the cryptographic
tenant binding: tenant scope comes ONLY from a verified access token's org claim —
never from headers, request bodies or any other caller-controlled input. Login-failure
enumeration and secret-leakage defenses run against the real database in
tests/integration/test_auth_security.py.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from starlette.requests import Request

from nexus_ai.api.tenancy import BearerTokenTenantContextResolver
from nexus_ai.core.errors import TokenValidationError
from nexus_ai.core.tenancy import TenantContextSource
from nexus_ai.domain.auth.keys import LocalEd25519KeyProvider
from nexus_ai.domain.auth.tokens import TokenService

pytestmark = pytest.mark.anyio

ISSUER = "nexus-ai"
AUDIENCE = "nexus-ai-backend"


def _tokens() -> TokenService:
    keys = LocalEd25519KeyProvider(Ed25519PrivateKey.generate())
    return TokenService(
        keys,
        issuer=ISSUER,
        audience=AUDIENCE,
        access_token_ttl_seconds=900,
        clock_skew_seconds=30,
    )


class _AcceptingStateGate:
    """Unit-test stub: the resolver's live-state gate always accepts (the state
    validation itself is proven against real PostgreSQL in the integration suite)."""

    async def require_valid(
        self, *, user_id: object, session_id: object, organization_id: object
    ) -> None:
        return None


def _resolver(tokens: TokenService | None = None) -> BearerTokenTenantContextResolver:
    return BearerTokenTenantContextResolver(tokens or _tokens(), _AcceptingStateGate())


def _request(headers: dict[str, str]) -> Request:
    # ASGI requires lowercase header names in scope (uvicorn lowercases them).
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "query_string": b"",
    }
    return Request(scope)


class TestResolverConfusedDeputy:
    async def test_no_authorization_header_resolves_no_context(self) -> None:
        assert await _resolver().resolve(_request({})) is None

    async def test_invalid_bearer_fails_closed(self) -> None:
        with pytest.raises(TokenValidationError):
            await _resolver().resolve(_request({"Authorization": "Bearer forged.token.here"}))

    async def test_non_bearer_scheme_fails_closed(self) -> None:
        with pytest.raises(TokenValidationError):
            await _resolver().resolve(_request({"Authorization": "Basic dXNlcjpwYXNz"}))

    async def test_empty_bearer_fails_closed(self) -> None:
        with pytest.raises(TokenValidationError):
            await _resolver().resolve(_request({"Authorization": "Bearer   "}))

    async def test_header_org_spoof_never_grants_scope(self) -> None:
        # The P02 spoof vector: a forged tenant header cannot create context.
        assert (
            await _resolver().resolve(_request({"X-NXS-Organization-ID": str(uuid.uuid7())}))
            is None
        )

    async def test_valid_token_derives_tenant_from_org_claim(self) -> None:
        tokens = _tokens()
        organization_id = uuid.uuid7()
        token = tokens.issue_access_token(
            subject=uuid.uuid7(),
            organization_id=organization_id,
            session_id=uuid.uuid7(),
            now=dt.datetime.now(dt.UTC),
        )
        context = await _resolver(tokens).resolve(_request({"Authorization": f"Bearer {token}"}))
        assert context is not None
        assert context.organization_id == organization_id
        assert context.source is TenantContextSource.RESOLVED_IDENTITY

    async def test_token_with_spoofed_header_still_uses_token_org(self) -> None:
        tokens = _tokens()
        organization_id = uuid.uuid7()
        token = tokens.issue_access_token(
            subject=uuid.uuid7(),
            organization_id=organization_id,
            session_id=uuid.uuid7(),
            now=dt.datetime.now(dt.UTC),
        )
        context = await _resolver(tokens).resolve(
            _request(
                {
                    "Authorization": f"Bearer {token}",
                    "X-NXS-Organization-ID": str(uuid.uuid7()),  # spoofed: ignored
                }
            )
        )
        assert context is not None
        assert context.organization_id == organization_id

    async def test_wrong_service_token_rejected(self) -> None:
        # A token signed by another deployment's key must not verify here.
        foreign = _tokens()
        token = foreign.issue_access_token(
            subject=uuid.uuid7(),
            organization_id=uuid.uuid7(),
            session_id=uuid.uuid7(),
            now=dt.datetime.now(dt.UTC),
        )
        with pytest.raises(TokenValidationError):
            await _resolver().resolve(_request({"Authorization": f"Bearer {token}"}))

    async def test_expired_token_rejected(self) -> None:
        tokens = _tokens()
        token = tokens.issue_access_token(
            subject=uuid.uuid7(),
            organization_id=uuid.uuid7(),
            session_id=uuid.uuid7(),
            now=dt.datetime.now(dt.UTC) - dt.timedelta(hours=2),
        )
        with pytest.raises(TokenValidationError):
            await _resolver(tokens).resolve(_request({"Authorization": f"Bearer {token}"}))


class TestSignedTokenTenantBinding:
    async def test_org_claim_cannot_be_changed_without_invalidating_signature(self) -> None:
        tokens = _tokens()
        real_org = uuid.uuid7()
        token = tokens.issue_access_token(
            subject=uuid.uuid7(),
            organization_id=real_org,
            session_id=uuid.uuid7(),
            now=dt.datetime.now(dt.UTC),
        )
        header, payload, signature = token.split(".")
        claims = json.loads(jwt.utils.base64url_decode(payload))
        claims["org"] = str(uuid.uuid7())  # swap tenant
        tampered = f"{header}.{jwt.utils.base64url_encode(json.dumps(claims).encode())}.{signature}"
        with pytest.raises(TokenValidationError):
            tokens.verify_access_token(tampered)

    async def test_subject_claim_cannot_be_swapped(self) -> None:
        tokens = _tokens()
        token = tokens.issue_access_token(
            subject=uuid.uuid7(),
            organization_id=uuid.uuid7(),
            session_id=uuid.uuid7(),
            now=dt.datetime.now(dt.UTC),
        )
        header, payload, signature = token.split(".")
        claims = json.loads(jwt.utils.base64url_decode(payload))
        claims["sub"] = str(uuid.uuid7())  # impersonate another user
        tampered = f"{header}.{jwt.utils.base64url_encode(json.dumps(claims).encode())}.{signature}"
        with pytest.raises(TokenValidationError):
            tokens.verify_access_token(tampered)
