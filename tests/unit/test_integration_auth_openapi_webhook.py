"""AuthProfile application, OpenAPI ingestion, outbound rate limiting and webhook
signature verification (NXS-INT-001)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

import httpx
import pytest

from nexus_ai.core.config import IntegrationsSettings
from nexus_ai.integrations.auth_profiles import AuthProfileApplier
from nexus_ai.integrations.credentials import CredentialType, InMemoryVault, SecretMaterial
from nexus_ai.integrations.destination import DestinationPolicy
from nexus_ai.integrations.entities import ApiKeyLocation, AuthProfile, AuthProfileType
from nexus_ai.integrations.errors import (
    IntegrationCredentialUnavailableError,
    IntegrationOpenApiInvalidError,
    IntegrationOutboundRateLimitedError,
    WebhookReplayError,
    WebhookSignatureInvalidError,
)
from nexus_ai.integrations.executor import GovernedHttpExecutor
from nexus_ai.integrations.openapi import OpenApiIngestor
from nexus_ai.integrations.ratelimit import OutboundRateLimiter
from nexus_ai.integrations.webhooks import (
    build_webhook_token,
    decode_webhook_token,
    verify_hmac_sha256,
)

pytestmark = pytest.mark.anyio

SETTINGS = IntegrationsSettings()


def _mock_executor(handler) -> GovernedHttpExecutor:  # type: ignore[no-untyped-def]
    policy = DestinationPolicy(allow_loopback=True, resolver=lambda h, p: ["127.0.0.1"])

    def factory(timeout: httpx.Timeout) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)

    return GovernedHttpExecutor(SETTINGS, policy, client_factory=factory)


async def _applier(vault: InMemoryVault, executor: GovernedHttpExecutor) -> AuthProfileApplier:
    return AuthProfileApplier(vault, DestinationPolicy(), executor)


async def test_auth_none_is_empty() -> None:
    applier = await _applier(InMemoryVault(), _mock_executor(lambda r: httpx.Response(200)))
    prepared = await applier.prepare(uuid.uuid4(), AuthProfile())
    assert prepared.headers == {} and prepared.query_params == {}


async def test_auth_api_key_header_and_query() -> None:
    org = uuid.uuid4()
    vault = InMemoryVault()
    await vault.store_secret(
        org, "k:1", SecretMaterial(CredentialType.API_KEY, {"api_key": "SEKRET"})
    )
    applier = await _applier(vault, _mock_executor(lambda r: httpx.Response(200)))
    header = await applier.prepare(
        org,
        AuthProfile(
            profile_type=AuthProfileType.API_KEY,
            credential_ref="k:1",
            header_name="X-Api-Key",
            value_prefix="Token ",
        ),
    )
    assert header.headers == {"X-Api-Key": "Token SEKRET"}
    query = await applier.prepare(
        org,
        AuthProfile(
            profile_type=AuthProfileType.API_KEY,
            credential_ref="k:1",
            api_key_location=ApiKeyLocation.QUERY,
            query_param_name="api_key",
        ),
    )
    assert query.query_params == {"api_key": "SEKRET"}


async def test_auth_bearer_and_basic() -> None:
    org = uuid.uuid4()
    vault = InMemoryVault()
    await vault.store_secret(
        org, "b:1", SecretMaterial(CredentialType.BEARER_TOKEN, {"token": "T"})
    )
    await vault.store_secret(
        org, "a:1", SecretMaterial(CredentialType.BASIC_AUTH, {"username": "u", "password": "p"})
    )
    applier = await _applier(vault, _mock_executor(lambda r: httpx.Response(200)))
    bearer = await applier.prepare(
        org, AuthProfile(profile_type=AuthProfileType.BEARER_TOKEN, credential_ref="b:1")
    )
    assert bearer.headers["Authorization"] == "Bearer T"
    basic = await applier.prepare(
        org, AuthProfile(profile_type=AuthProfileType.BASIC, credential_ref="a:1")
    )
    assert basic.headers["Authorization"].startswith("Basic ")


async def test_auth_wrong_credential_type_fails_closed() -> None:
    org = uuid.uuid4()
    vault = InMemoryVault()
    await vault.store_secret(org, "b:1", SecretMaterial(CredentialType.API_KEY, {"api_key": "x"}))
    applier = await _applier(vault, _mock_executor(lambda r: httpx.Response(200)))
    with pytest.raises(IntegrationCredentialUnavailableError, match="wrong type"):
        await applier.prepare(
            org, AuthProfile(profile_type=AuthProfileType.BEARER_TOKEN, credential_ref="b:1")
        )


async def test_oauth2_client_credentials_fetches_and_caches() -> None:
    org = uuid.uuid4()
    vault = InMemoryVault()
    await vault.store_secret(
        org,
        "o:1",
        SecretMaterial(CredentialType.OAUTH2_CLIENT, {"client_id": "id", "client_secret": "s"}),
    )
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert b"grant_type=client_credentials" in request.content
        return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})

    applier = await _applier(vault, _mock_executor(handler))
    profile = AuthProfile(
        profile_type=AuthProfileType.OAUTH2_CLIENT_CREDENTIALS,
        credential_ref="o:1",
        token_url="https://auth.integration.test/token",
        oauth_scope="read",
    )
    first = await applier.prepare(org, profile)
    second = await applier.prepare(org, profile)
    assert first.headers["Authorization"] == "Bearer AT"
    assert second.headers["Authorization"] == "Bearer AT"
    assert calls["n"] == 1  # cached


async def test_oauth2_rejects_bad_token_response() -> None:
    org = uuid.uuid4()
    vault = InMemoryVault()
    await vault.store_secret(
        org,
        "o:1",
        SecretMaterial(CredentialType.OAUTH2_CLIENT, {"client_id": "i", "client_secret": "s"}),
    )
    applier = await _applier(vault, _mock_executor(lambda r: httpx.Response(401)))
    with pytest.raises(IntegrationCredentialUnavailableError):
        await applier.prepare(
            org,
            AuthProfile(
                profile_type=AuthProfileType.OAUTH2_CLIENT_CREDENTIALS,
                credential_ref="o:1",
                token_url="https://auth.integration.test/token",
            ),
        )


# --- OpenAPI ingestion ------------------------------------------------------------

_VALID_OPENAPI = {
    "openapi": "3.0.3",
    "info": {"title": "Demo", "version": "1.0"},
    "servers": [{"url": "https://api.example.com"}],
    "paths": {
        "/contacts/{id}": {
            "get": {
                "operationId": "getContact",
                "summary": "Get a contact",
                "parameters": [{"name": "id", "in": "path", "required": True}],
            }
        },
        "/contacts": {"post": {"operationId": "createContact", "requestBody": {"content": {}}}},
    },
}


def test_openapi_ingest_extracts_candidates() -> None:
    ingestor = OpenApiIngestor(SETTINGS, DestinationPolicy())
    result = ingestor.ingest(json.dumps(_VALID_OPENAPI).encode())
    assert result.title == "Demo" and result.server_url == "https://api.example.com"
    keys = {op.operation_key for op in result.operations}
    assert keys == {"getcontact", "createcontact"}
    get = next(op for op in result.operations if op.operation_key == "getcontact")
    assert "id" in get.spec.path_params


def test_openapi_rejects_remote_ref() -> None:
    import copy

    doc = copy.deepcopy(_VALID_OPENAPI)
    doc["paths"] = {"/x": {"get": {"parameters": [{"$ref": "https://evil/x.json"}]}}}
    with pytest.raises(IntegrationOpenApiInvalidError, match="local in-document"):
        OpenApiIngestor(SETTINGS, DestinationPolicy()).ingest(json.dumps(doc).encode())


def test_openapi_rejects_callbacks_and_bad_version() -> None:
    import copy

    with pytest.raises(IntegrationOpenApiInvalidError, match=r"3\.x"):
        OpenApiIngestor(SETTINGS, DestinationPolicy()).ingest(b'{"openapi": "2.0", "paths": {}}')
    doc = copy.deepcopy(_VALID_OPENAPI)
    doc["openapi"] = "3.1.0"
    doc["paths"]["/cb"] = {"post": {"callbacks": {"x": {}}}}
    with pytest.raises(IntegrationOpenApiInvalidError, match="callbacks"):
        OpenApiIngestor(SETTINGS, DestinationPolicy()).ingest(json.dumps(doc).encode())


def test_openapi_rejects_unsafe_server() -> None:
    import copy

    doc = copy.deepcopy(_VALID_OPENAPI)
    doc["servers"] = [{"url": "http://169.254.169.254/"}]
    with pytest.raises(Exception, match="blocked"):
        OpenApiIngestor(SETTINGS, DestinationPolicy()).ingest(json.dumps(doc).encode())


def test_openapi_operation_cap() -> None:
    tight = IntegrationsSettings(openapi_max_operations=1)
    with pytest.raises(IntegrationOpenApiInvalidError, match="too many operations"):
        OpenApiIngestor(tight, DestinationPolicy()).ingest(json.dumps(_VALID_OPENAPI).encode())


def test_openapi_size_and_empty() -> None:
    tiny = IntegrationsSettings(openapi_max_document_bytes=1024)
    with pytest.raises(IntegrationOpenApiInvalidError, match="size limit"):
        OpenApiIngestor(tiny, DestinationPolicy()).ingest(b"x" * 2048)
    doc = {"openapi": "3.0.0", "info": {}, "paths": {}}
    with pytest.raises(IntegrationOpenApiInvalidError, match="no importable"):
        OpenApiIngestor(SETTINGS, DestinationPolicy()).ingest(json.dumps(doc).encode())


# --- outbound rate limiting -------------------------------------------------------


class _FakeCache:
    is_connected = False
    client = None


async def test_outbound_rate_limiter_local_window() -> None:
    clock = {"t": 1000.0}
    limiter = OutboundRateLimiter(
        IntegrationsSettings(outbound_rate_limit_per_minute=2), _FakeCache(), now=lambda: clock["t"]
    )
    org = uuid.uuid4()
    integration = uuid.uuid4()
    await limiter.check_and_consume(org, integration)
    await limiter.check_and_consume(org, integration)
    with pytest.raises(IntegrationOutboundRateLimitedError):
        await limiter.check_and_consume(org, integration)
    clock["t"] += 61
    await limiter.check_and_consume(org, integration)  # new window


# --- webhook token + signature -------------------------------------------------


def test_webhook_token_roundtrip() -> None:
    org = uuid.uuid4()
    token = build_webhook_token(org)
    assert decode_webhook_token(token) == org


def test_webhook_token_malformed() -> None:
    from nexus_ai.integrations.errors import WebhookEndpointNotFoundError

    with pytest.raises(WebhookEndpointNotFoundError):
        decode_webhook_token("garbage")
    with pytest.raises(WebhookEndpointNotFoundError):
        decode_webhook_token("!!!.abc")


def test_verify_hmac_sha256_valid_and_invalid() -> None:
    secret = "whsec"
    body = b'{"event": "x"}'
    ts = str(int(time.time()))
    good = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    verify_hmac_sha256(
        body=body,
        provided_signature=f"sha256={good}",
        secret=secret,
        timestamp=ts,
        tolerance_seconds=300,
    )
    with pytest.raises(WebhookSignatureInvalidError, match="unsigned"):
        verify_hmac_sha256(
            body=body,
            provided_signature=None,
            secret=secret,
            timestamp=ts,
            tolerance_seconds=300,
        )
    with pytest.raises(WebhookSignatureInvalidError, match="did not verify"):
        verify_hmac_sha256(
            body=body,
            provided_signature="sha256=deadbeef",
            secret=secret,
            timestamp=ts,
            tolerance_seconds=300,
        )
    with pytest.raises(WebhookReplayError):
        verify_hmac_sha256(
            body=body,
            provided_signature=f"sha256={good}",
            secret=secret,
            timestamp=str(int(time.time()) - 9999),
            tolerance_seconds=300,
        )


def test_verify_hmac_without_timestamp() -> None:
    secret = "s"
    body = b"raw"
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    verify_hmac_sha256(
        body=body, provided_signature=sig, secret=secret, timestamp=None, tolerance_seconds=300
    )
