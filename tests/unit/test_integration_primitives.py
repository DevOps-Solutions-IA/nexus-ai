"""Integration Hub value objects, credentials, backoff, circuit, rate limit, result
validation and REST/GraphQL request building (NXS-INT-001)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from cryptography.fernet import Fernet

from nexus_ai.core.config import IntegrationsSettings
from nexus_ai.integrations.backoff import FailureKind, RetryPolicy
from nexus_ai.integrations.circuit import CircuitBreakerRegistry
from nexus_ai.integrations.credentials import (
    CredentialType,
    InMemoryVault,
    LocalEncryptedVault,
    SecretMaterial,
    build_fernet,
)
from nexus_ai.integrations.entities import (
    ApiKeyLocation,
    AuthProfile,
    AuthProfileType,
    CircuitState,
    GraphQLOperationSpec,
    HttpMethod,
    ParamSpec,
    RestOperationSpec,
    RetryClass,
)
from nexus_ai.integrations.errors import (
    IntegrationCredentialUnavailableError,
    IntegrationOperationInputInvalidError,
    IntegrationResponseInvalidError,
    IntegrationUpstreamClientError,
    IntegrationUpstreamRateLimitedError,
    IntegrationUpstreamServerError,
)
from nexus_ai.integrations.executor import RawResponse
from nexus_ai.integrations.graphql import GraphQLInvocationBuilder, analyse_document
from nexus_ai.integrations.rest import RestInvocationBuilder
from nexus_ai.integrations.result import validate_graphql_response, validate_rest_response

pytestmark = pytest.mark.anyio

SETTINGS = IntegrationsSettings()


# --- AuthProfile validation --------------------------------------------------------


def test_auth_profile_none_rejects_credential() -> None:
    with pytest.raises(ValueError, match="must not carry a credential_ref"):
        AuthProfile(profile_type=AuthProfileType.NONE, credential_ref="x:1")


def test_auth_profile_requires_credential() -> None:
    with pytest.raises(ValueError, match="requires a credential_ref"):
        AuthProfile(profile_type=AuthProfileType.BEARER_TOKEN)


def test_auth_profile_rejects_reserved_header() -> None:
    with pytest.raises(ValueError, match="reserved"):
        AuthProfile(
            profile_type=AuthProfileType.API_KEY,
            credential_ref="k:1",
            header_name="Authorization",
        )


def test_auth_profile_query_key_needs_param_name() -> None:
    with pytest.raises(ValueError, match="query-based key"):
        AuthProfile(
            profile_type=AuthProfileType.API_KEY,
            credential_ref="k:1",
            api_key_location=ApiKeyLocation.QUERY,
        )


def test_oauth2_requires_token_url() -> None:
    with pytest.raises(ValueError, match="token_url"):
        AuthProfile(profile_type=AuthProfileType.OAUTH2_CLIENT_CREDENTIALS, credential_ref="k:1")


# --- RestOperationSpec validation -------------------------------------------------


def test_rest_spec_path_params_must_match_template() -> None:
    with pytest.raises(ValueError, match="path template"):
        RestOperationSpec(method=HttpMethod.GET, path="/x/{id}", path_params={})


def test_rest_spec_get_rejects_body_schema() -> None:
    with pytest.raises(ValueError, match="GET operation must not"):
        RestOperationSpec(method=HttpMethod.GET, path="/x", body_schema={"type": "object"})


def test_rest_spec_rejects_reserved_operation_header() -> None:
    with pytest.raises(ValueError, match="reserved"):
        RestOperationSpec(method=HttpMethod.GET, path="/x", header_params={"Host": ParamSpec()})


# --- SecretMaterial + vault ----------------------------------------------------


def test_secret_material_redacts() -> None:
    material = SecretMaterial(CredentialType.API_KEY, {"api_key": "super-secret"})
    assert "super-secret" not in repr(material)
    assert "super-secret" not in str(material)
    assert material.field("api_key") == "super-secret"


def test_secret_material_requires_fields() -> None:
    with pytest.raises(IntegrationCredentialUnavailableError):
        SecretMaterial(CredentialType.BASIC_AUTH, {"username": "u"})


async def test_local_encrypted_vault_roundtrip() -> None:
    org = uuid.uuid4()
    vault = LocalEncryptedVault(InMemoryStore(), build_fernet([Fernet.generate_key().decode()]))
    await vault.store_secret(
        org, "hub:key", SecretMaterial(CredentialType.API_KEY, {"api_key": "abc123"})
    )
    resolved = await vault.get_secret(org, "hub:key")
    assert resolved.field("api_key") == "abc123"
    assert await vault.has_secret(org, "hub:key")
    await vault.delete_secret(org, "hub:key")
    with pytest.raises(IntegrationCredentialUnavailableError):
        await vault.get_secret(org, "hub:key")


async def test_local_vault_rejects_tampered_ciphertext() -> None:
    org = uuid.uuid4()
    store = InMemoryStore()
    vault = LocalEncryptedVault(store, build_fernet([Fernet.generate_key().decode()]))
    await vault.store_secret(
        org, "hub:key", SecretMaterial(CredentialType.HMAC_SECRET, {"secret": "s"})
    )
    from nexus_ai.integrations.credentials import EncryptedSecret

    store.rows[(org, "hub:key")] = EncryptedSecret(CredentialType.HMAC_SECRET, "not-valid-fernet")
    with pytest.raises(IntegrationCredentialUnavailableError, match="could not be decrypted"):
        await vault.get_secret(org, "hub:key")


async def test_local_vault_wrong_key_fails_closed() -> None:
    org = uuid.uuid4()
    store = InMemoryStore()
    await LocalEncryptedVault(store, build_fernet([Fernet.generate_key().decode()])).store_secret(
        org, "hub:key", SecretMaterial(CredentialType.API_KEY, {"api_key": "x"})
    )
    other = LocalEncryptedVault(store, build_fernet([Fernet.generate_key().decode()]))
    with pytest.raises(IntegrationCredentialUnavailableError):
        await other.get_secret(org, "hub:key")


def test_build_fernet_rejects_bad_key() -> None:
    with pytest.raises(IntegrationCredentialUnavailableError):
        build_fernet(["not-base64"])
    with pytest.raises(IntegrationCredentialUnavailableError):
        build_fernet([])


async def test_in_memory_vault() -> None:
    org = uuid.uuid4()
    vault = InMemoryVault()
    assert not await vault.has_secret(org, "a:1")
    await vault.store_secret(
        org, "a:1", SecretMaterial(CredentialType.BEARER_TOKEN, {"token": "t"})
    )
    assert (await vault.get_secret(org, "a:1")).field("token") == "t"


class InMemoryStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[uuid.UUID, str], object] = {}

    async def get(self, organization_id: uuid.UUID, ref: str) -> object | None:
        return self.rows.get((organization_id, ref))

    async def put(self, organization_id: uuid.UUID, ref: str, secret: object) -> None:
        self.rows[(organization_id, ref)] = secret

    async def delete(self, organization_id: uuid.UUID, ref: str) -> bool:
        return self.rows.pop((organization_id, ref), None) is not None


# --- RetryPolicy -----------------------------------------------------------------


def test_retry_policy_classes() -> None:
    policy = RetryPolicy(IntegrationsSettings(retry_max_attempts=4))
    # NON_IDEMPOTENT: only PRE_SEND retries
    assert policy.decide(
        retry_class=RetryClass.NON_IDEMPOTENT,
        failure=FailureKind.PRE_SEND,
        attempt=1,
        elapsed_seconds=0,
    ).should_retry
    assert not policy.decide(
        retry_class=RetryClass.NON_IDEMPOTENT,
        failure=FailureKind.UPSTREAM_5XX,
        attempt=1,
        elapsed_seconds=0,
    ).should_retry
    assert not policy.decide(
        retry_class=RetryClass.NON_IDEMPOTENT,
        failure=FailureKind.IN_FLIGHT,
        attempt=1,
        elapsed_seconds=0,
    ).should_retry
    # SAFE: retries anything transient
    assert policy.decide(
        retry_class=RetryClass.SAFE,
        failure=FailureKind.UPSTREAM_5XX,
        attempt=1,
        elapsed_seconds=0,
    ).should_retry
    # IDEMPOTENT: retries in-flight + 5xx + 429
    assert policy.decide(
        retry_class=RetryClass.IDEMPOTENT,
        failure=FailureKind.UPSTREAM_429,
        attempt=1,
        elapsed_seconds=0,
    ).should_retry
    # terminal never
    assert not policy.decide(
        retry_class=RetryClass.SAFE, failure=FailureKind.TERMINAL, attempt=1, elapsed_seconds=0
    ).should_retry
    # max attempts
    assert not policy.decide(
        retry_class=RetryClass.SAFE, failure=FailureKind.PRE_SEND, attempt=4, elapsed_seconds=0
    ).should_retry
    # elapsed budget
    assert not policy.decide(
        retry_class=RetryClass.SAFE,
        failure=FailureKind.PRE_SEND,
        attempt=1,
        elapsed_seconds=999,
    ).should_retry


def test_retry_after_is_clamped() -> None:
    policy = RetryPolicy(
        IntegrationsSettings(retry_max_delay_seconds=5, retry_max_elapsed_seconds=100)
    )
    decision = policy.decide(
        retry_class=RetryClass.IDEMPOTENT,
        failure=FailureKind.UPSTREAM_429,
        attempt=1,
        elapsed_seconds=0,
        retry_after_seconds=999,
    )
    assert decision.should_retry and decision.delay_seconds == 5


# --- CircuitBreakerRegistry ----------------------------------------------------


async def test_circuit_opens_after_threshold_and_recovers() -> None:
    clock = {"t": 0.0}
    reg = CircuitBreakerRegistry(
        IntegrationsSettings(circuit_failure_threshold=2, circuit_reset_seconds=10),
        now=lambda: clock["t"],
    )
    key = (uuid.uuid4(), uuid.uuid4(), "op")
    await reg.before_call(key)
    await reg.record_failure(key)
    await reg.record_failure(key)
    assert await reg.state_of(key) is CircuitState.OPEN
    with pytest.raises(Exception, match="circuit is open"):
        await reg.before_call(key)
    clock["t"] = 20.0
    await reg.before_call(key)  # transitions to HALF_OPEN
    assert await reg.state_of(key) is CircuitState.HALF_OPEN
    await reg.record_success(key)
    assert await reg.state_of(key) is CircuitState.CLOSED


async def test_circuit_half_open_failure_reopens() -> None:
    clock = {"t": 0.0}
    reg = CircuitBreakerRegistry(
        IntegrationsSettings(circuit_failure_threshold=1, circuit_reset_seconds=5),
        now=lambda: clock["t"],
    )
    key = (uuid.uuid4(), uuid.uuid4(), "op")
    await reg.record_failure(key)
    clock["t"] = 10.0
    await reg.before_call(key)
    await reg.record_failure(key)
    assert await reg.state_of(key) is CircuitState.OPEN


# --- result validation -------------------------------------------------------


def _resp(status: int, body: bytes = b"", ctype: str = "application/json") -> RawResponse:
    return RawResponse(status, {"content-type": ctype}, body, 1, "https://x/y")


def test_validate_rest_success_and_schema() -> None:
    spec = RestOperationSpec(
        method=HttpMethod.GET,
        path="/x",
        response_schema={"type": "object", "required": ["id"]},
    )
    assert validate_rest_response(_resp(200, b'{"id": 1}'), spec) == {"id": 1}
    with pytest.raises(IntegrationResponseInvalidError):
        validate_rest_response(_resp(200, b'{"nope": 1}'), spec)


def test_validate_rest_status_mapping() -> None:
    spec = RestOperationSpec(method=HttpMethod.GET, path="/x")
    with pytest.raises(IntegrationUpstreamServerError):
        validate_rest_response(_resp(503), spec)
    with pytest.raises(IntegrationUpstreamClientError):
        validate_rest_response(_resp(404), spec)
    with pytest.raises(IntegrationUpstreamRateLimitedError):
        validate_rest_response(_resp(429), spec)


def test_validate_rest_content_type_and_json() -> None:
    spec = RestOperationSpec(method=HttpMethod.GET, path="/x")
    with pytest.raises(IntegrationResponseInvalidError, match="content-type"):
        validate_rest_response(_resp(200, b"<html>", ctype="text/html"), spec)
    with pytest.raises(IntegrationResponseInvalidError, match="not valid JSON"):
        validate_rest_response(_resp(200, b"not json"), spec)


def test_validate_rest_204_is_none() -> None:
    spec = RestOperationSpec(method=HttpMethod.DELETE, path="/x", success_status=(204,))
    assert validate_rest_response(_resp(204), spec) is None


def test_validate_graphql() -> None:
    assert validate_graphql_response(_resp(200, b'{"data": {"x": 1}}'), None) == {"x": 1}
    with pytest.raises(IntegrationResponseInvalidError, match="errors"):
        validate_graphql_response(_resp(200, b'{"errors": [{"message": "boom"}]}'), None)
    with pytest.raises(IntegrationUpstreamServerError):
        validate_graphql_response(_resp(500), None)


# --- REST request building ---------------------------------------------------


def test_rest_builder_path_and_query() -> None:
    spec = RestOperationSpec(
        method=HttpMethod.GET,
        path="/v1/contacts/{id}",
        path_params={"id": ParamSpec(required=True, pattern=r"[0-9]+")},
        query_params={"expand": ParamSpec(), "version": ParamSpec(constant="2024-01")},
    )
    req = RestInvocationBuilder().build(
        "https://api.example.com",
        spec,
        {"path_params": {"id": "42"}, "query_params": {"expand": "owner"}},
    )
    assert req.url == "https://api.example.com/v1/contacts/42?expand=owner&version=2024-01"
    assert req.method == "GET"


def test_rest_builder_rejects_unknown_param_and_bad_pattern() -> None:
    spec = RestOperationSpec(
        method=HttpMethod.GET, path="/x", query_params={"a": ParamSpec(pattern=r"[0-9]+")}
    )
    b = RestInvocationBuilder()
    with pytest.raises(IntegrationOperationInputInvalidError, match="unknown query"):
        b.build("https://api.example.com", spec, {"query_params": {"bogus": "1"}})
    with pytest.raises(IntegrationOperationInputInvalidError, match="pattern"):
        b.build("https://api.example.com", spec, {"query_params": {"a": "xx"}})


def test_rest_builder_body_schema() -> None:
    spec = RestOperationSpec(
        method=HttpMethod.POST,
        path="/x",
        body_schema={
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}},
        },
    )
    b = RestInvocationBuilder()
    req = b.build("https://api.example.com", spec, {"body": {"name": "z"}})
    assert req.body == b'{"name":"z"}'
    with pytest.raises(IntegrationOperationInputInvalidError, match="schema validation"):
        b.build("https://api.example.com", spec, {"body": {"name": 1}})
    with pytest.raises(IntegrationOperationInputInvalidError, match="requires a request body"):
        b.build("https://api.example.com", spec, {})


def test_rest_builder_get_rejects_body() -> None:
    spec = RestOperationSpec(method=HttpMethod.GET, path="/x")
    with pytest.raises(IntegrationOperationInputInvalidError, match="does not accept"):
        RestInvocationBuilder().build("https://api.example.com", spec, {"body": {"a": 1}})


def test_rest_builder_missing_required_query() -> None:
    spec = RestOperationSpec(
        method=HttpMethod.GET, path="/x", query_params={"a": ParamSpec(required=True)}
    )
    with pytest.raises(IntegrationOperationInputInvalidError, match="required"):
        RestInvocationBuilder().build("https://api.example.com", spec, {})


# --- GraphQL ---------------------------------------------------------------


def test_graphql_analyse_rejects_introspection_and_deep() -> None:
    with pytest.raises(Exception, match="introspection"):
        analyse_document("query { __schema { types { name } } }", max_depth_limit=10)
    with pytest.raises(Exception, match="subscription"):
        analyse_document("subscription { onX { id } }", max_depth_limit=10)
    with pytest.raises(Exception, match="depth"):
        analyse_document("query { a { b { c { d { e } } } } }", max_depth_limit=2)


def test_graphql_analyse_detects_mutation() -> None:
    info = analyse_document("mutation { createX(input: {}) { id } }", max_depth_limit=10)
    assert info.has_mutation


def test_graphql_builder_variables_schema() -> None:
    spec = GraphQLOperationSpec(
        document="query Q($id: ID!) { node(id: $id) { id } }",
        operation_name="Q",
        variables_schema={
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "string"}},
        },
    )
    b = GraphQLInvocationBuilder()
    req = b.build("https://gql.example.com/graphql", spec, {"variables": {"id": "abc"}})
    assert b'"operationName":"Q"' in req.body  # type: ignore[operator]
    with pytest.raises(IntegrationOperationInputInvalidError):
        b.build("https://gql.example.com/graphql", spec, {"variables": {}})
    with pytest.raises(IntegrationOperationInputInvalidError, match="unexpected"):
        b.build("https://gql.example.com/graphql", spec, {"nope": 1})


def test_datetime_import_kept_for_ruff() -> None:
    assert isinstance(dt.datetime.now(dt.UTC), dt.datetime)
