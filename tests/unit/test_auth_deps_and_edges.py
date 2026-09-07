"""Edge coverage for auth dependencies, token parsing and database guards."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from starlette.requests import Request

from nexus_ai.api.auth_deps import _bearer_token
from nexus_ai.core.errors import (
    AuthenticationRequiredError,
    ConfigurationError,
    TokenValidationError,
)
from nexus_ai.domain.auth.keys import LocalEd25519KeyProvider
from nexus_ai.domain.auth.tokens import TokenService, parse_refresh_token
from nexus_ai.infrastructure.database import Database

pytestmark = pytest.mark.anyio


def _request(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "query_string": b"",
    }
    return Request(scope)


class TestBearerTokenDependency:
    def test_missing_header_raises_authentication_required(self) -> None:
        with pytest.raises(AuthenticationRequiredError):
            _bearer_token(_request({}))

    def test_non_bearer_scheme_fails_closed(self) -> None:
        with pytest.raises(TokenValidationError):
            _bearer_token(_request({"Authorization": "Digest abc"}))

    def test_bearer_token_extracted(self) -> None:
        assert _bearer_token(_request({"Authorization": "Bearer abc.def.ghi"})) == "abc.def.ghi"


class TestRefreshTokenEdges:
    def test_overlong_refresh_token_fails_closed(self) -> None:
        with pytest.raises(TokenValidationError):
            parse_refresh_token("a" * 300)

    def test_prefix_without_random_part_fails_closed(self) -> None:
        from nexus_ai.domain.auth.tokens import new_refresh_token

        prefix, _ = new_refresh_token(uuid.uuid7()).split(".", 1)
        with pytest.raises(TokenValidationError):
            parse_refresh_token(f"{prefix}.")


class TestTokenServiceEdges:
    def _tokens(self) -> TokenService:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        keys = LocalEd25519KeyProvider(Ed25519PrivateKey.generate())
        return TokenService(
            keys,
            issuer="i",
            audience="a",
            access_token_ttl_seconds=900,
            clock_skew_seconds=30,
        )

    def test_verify_rejects_non_string_kid(self) -> None:
        import json

        import jwt

        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        keys = LocalEd25519KeyProvider(Ed25519PrivateKey.generate())
        signing = keys.signing_key()
        now = dt.datetime.now(dt.UTC)
        payload = jwt.utils.base64url_encode(
            json.dumps(
                {
                    "iss": "i",
                    "aud": "a",
                    "sub": str(uuid.uuid7()),
                    "org": str(uuid.uuid7()),
                    "sid": str(uuid.uuid7()),
                    "jti": str(uuid.uuid7()),
                    "iat": int(now.timestamp()),
                    "nbf": int(now.timestamp()),
                    "exp": int((now + dt.timedelta(seconds=900)).timestamp()),
                }
            ).encode()
        )
        # Craft the raw JWS: a NON-STRING kid must fail closed at verification.
        header = jwt.utils.base64url_encode(b'{"alg":"EdDSA","kid":12345,"typ":"access"}')
        forged = f"{header}.{payload}.{jwt.utils.base64url_encode(b'signature')}"
        with pytest.raises(TokenValidationError):
            self._tokens().verify_access_token(forged)

    def test_verify_rejects_non_numeric_time_claims(self) -> None:
        import jwt

        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        keys = LocalEd25519KeyProvider(Ed25519PrivateKey.generate())
        signing = keys.signing_key()
        forged = jwt.encode(
            {
                "iss": "i",
                "aud": "a",
                "sub": str(uuid.uuid7()),
                "org": str(uuid.uuid7()),
                "sid": str(uuid.uuid7()),
                "jti": str(uuid.uuid7()),
                "iat": "not-a-number",
                "nbf": "not-a-number",
                "exp": "not-a-number",
            },
            signing.private_key,
            algorithm="EdDSA",
            headers={"kid": signing.kid, "typ": "access"},
        )
        with pytest.raises(TokenValidationError):
            self._tokens().verify_access_token(forged)


class TestDatabaseGuards:
    def test_engine_property_requires_connection(self) -> None:
        from nexus_ai.core.config import DatabaseSettings

        database = Database(DatabaseSettings(required=False))
        with pytest.raises(ConfigurationError):
            _ = database.engine

    def test_principal_session_rejects_non_uuid(self) -> None:
        from nexus_ai.core.config import DatabaseSettings
        from nexus_ai.core.errors import TenantContextInvalidError

        database = Database(DatabaseSettings(required=False))

        async def _use() -> None:
            async with database.principal_session("not-a-uuid"):  # type: ignore[arg-type]
                pass

        with pytest.raises(TenantContextInvalidError):
            import asyncio

            asyncio.run(_use())

    def test_tenant_transaction_rejects_non_uuid(self) -> None:
        from nexus_ai.core.config import DatabaseSettings
        from nexus_ai.core.errors import TenantContextInvalidError

        database = Database(DatabaseSettings(required=False))

        async def _use() -> None:
            async with database.tenant_transaction("not-a-uuid"):  # type: ignore[arg-type]
                pass

        with pytest.raises(TenantContextInvalidError):
            import asyncio

            asyncio.run(_use())
