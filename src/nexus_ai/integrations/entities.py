"""Integration Hub domain values and API contracts (NXS-INT-001).

ORM rows never cross the service boundary. Nothing here — request model, stored config
or result — ever carries plaintext secret material: an :class:`AuthProfile` references a
secret only through an opaque ``credential_ref`` resolved by the vault seam
(:mod:`nexus_ai.integrations.credentials`). Every model is strict (``extra="forbid"``)
and bounded.

The execution contract is operation-based: a caller supplies an ``integration_id``, an
``operation_key`` and a validated ``input`` object — never a raw URL, method, header set,
query string or GraphQL document. Those live only in the tenant-owned, config-versioned
registry.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

# --- bounded string aliases ---------------------------------------------------------

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{1,62}$")]
OperationKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{1,62}$")]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]
HeaderName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")]
ParamName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,62}$")]
CredentialRef = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_:-]{2,126}$")]

#: Request headers the caller, an operation config or an adapter may NEVER set. The
#: governed executor and the auth layer own these; an attempt to inject one is a config
#: error. The configured sensitive-header names are added to this set at runtime.
RESERVED_REQUEST_HEADERS: frozenset[str] = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "host",
        "content-length",
        "connection",
        "transfer-encoding",
        "keep-alive",
        "upgrade",
        "te",
        "trailer",
        "cookie",
        "expect",
    }
)


# --- enums ------------------------------------------------------------------------


class IntegrationType(StrEnum):
    REST = "REST"
    OPENAPI = "OPENAPI"
    GRAPHQL = "GRAPHQL"
    CRM = "CRM"
    ERP = "ERP"
    CALENDAR = "CALENDAR"
    WEBHOOK = "WEBHOOK"


class IntegrationStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    ERROR = "ERROR"


class AuthProfileType(StrEnum):
    NONE = "NONE"
    API_KEY = "API_KEY"
    BEARER_TOKEN = "BEARER_TOKEN"  # noqa: S105 - enum member name, not a secret
    BASIC = "BASIC"
    OAUTH2_CLIENT_CREDENTIALS = "OAUTH2_CLIENT_CREDENTIALS"
    CUSTOM_HEADER = "CUSTOM_HEADER"


class ApiKeyLocation(StrEnum):
    HEADER = "HEADER"
    QUERY = "QUERY"


class HttpMethod(StrEnum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"


class OperationType(StrEnum):
    REST = "REST"
    GRAPHQL = "GRAPHQL"


class RetryClass(StrEnum):
    """How safely an operation may be retried after a transport or 5xx failure."""

    SAFE = "SAFE"  # no side effects (typically GET) — always retryable
    IDEMPOTENT = "IDEMPOTENT"  # side effects, but a repeat is harmless (PUT, DELETE)
    NON_IDEMPOTENT = "NON_IDEMPOTENT"  # a repeat may double an effect (POST) — retry only pre-send


class IdempotencyMode(StrEnum):
    NONE = "NONE"
    CALLER_KEY = "CALLER_KEY"  # dedup on the caller-supplied idempotency key
    PROVIDER_KEY = "PROVIDER_KEY"  # also forward the key to the provider's idempotency header


class ResultClass(StrEnum):
    SUCCESS = "SUCCESS"
    UPSTREAM_CLIENT_ERROR = "UPSTREAM_CLIENT_ERROR"
    UPSTREAM_SERVER_ERROR = "UPSTREAM_SERVER_ERROR"
    UPSTREAM_RATE_LIMITED = "UPSTREAM_RATE_LIMITED"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    RESPONSE_INVALID = "RESPONSE_INVALID"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    RATE_LIMITED = "RATE_LIMITED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    CONFIG_ERROR = "CONFIG_ERROR"
    EXECUTION_FAILED = "EXECUTION_FAILED"


class CircuitState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class WebhookSignatureScheme(StrEnum):
    NONE = "NONE"
    HMAC_SHA256 = "HMAC_SHA256"


# --- destination policy ----------------------------------------------------------


class DestinationRule(BaseModel):
    """Per-integration additions to the global SSRF destination policy. The global policy
    (block loopback / link-local / metadata / private / reserved, http(s) only) is always
    enforced; this can only make it STRICTER, never looser."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Force https for THIS integration even where the environment would allow http.
    #: The environment-level requirement (hardened envs) is always applied on top.
    require_https: bool = False
    #: Extra hostnames to block (exact, case-insensitive). Never an allow-list — the
    #: global policy already constrains destinations; this narrows further.
    blocked_hosts: tuple[Annotated[str, StringConstraints(max_length=253)], ...] = ()
    #: Extra CIDR ranges to block, in addition to the always-blocked private/reserved set.
    blocked_cidrs: tuple[Annotated[str, StringConstraints(max_length=64)], ...] = ()


# --- auth profile ---------------------------------------------------------------


class AuthProfile(BaseModel):
    """Typed authentication configuration. The secret is referenced, never embedded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_type: AuthProfileType = AuthProfileType.NONE
    credential_ref: CredentialRef | None = None
    #: API_KEY / CUSTOM_HEADER: the header to inject (rejected if reserved).
    header_name: HeaderName | None = None
    #: API_KEY: where the key goes.
    api_key_location: ApiKeyLocation = ApiKeyLocation.HEADER
    #: API_KEY in QUERY: the query parameter name.
    query_param_name: ParamName | None = None
    #: API_KEY header value prefix, e.g. "Token " (bounded, no control chars).
    value_prefix: Annotated[str, StringConstraints(max_length=32)] | None = None
    #: OAUTH2_CLIENT_CREDENTIALS: the token endpoint (itself SSRF-validated).
    token_url: Annotated[str, StringConstraints(max_length=2048)] | None = None
    oauth_scope: Annotated[str, StringConstraints(max_length=500)] | None = None

    @model_validator(mode="after")
    def _coherent(self) -> AuthProfile:
        needs_credential = self.profile_type is not AuthProfileType.NONE
        if needs_credential and self.credential_ref is None:
            raise ValueError(f"{self.profile_type} requires a credential_ref")
        if self.profile_type is AuthProfileType.NONE and self.credential_ref is not None:
            raise ValueError("auth profile NONE must not carry a credential_ref")
        if self.profile_type in (AuthProfileType.API_KEY, AuthProfileType.CUSTOM_HEADER):
            if self.api_key_location is ApiKeyLocation.HEADER and self.header_name is None:
                raise ValueError("a header-based key requires header_name")
            if self.header_name and self.header_name.lower() in RESERVED_REQUEST_HEADERS:
                raise ValueError(f"header_name {self.header_name!r} is reserved")
            if self.api_key_location is ApiKeyLocation.QUERY and self.query_param_name is None:
                raise ValueError("a query-based key requires query_param_name")
        if self.profile_type is AuthProfileType.OAUTH2_CLIENT_CREDENTIALS and not self.token_url:
            raise ValueError("OAUTH2_CLIENT_CREDENTIALS requires token_url")
        return self


# --- operation specs ----------------------------------------------------------


class ParamSpec(BaseModel):
    """A single allow-listed parameter (path / query / header value)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    required: bool = False
    #: A bounded regex the string form of the value must fully match.
    pattern: Annotated[str, StringConstraints(max_length=200)] | None = None
    max_length: Annotated[int, Field(ge=1, le=4096)] = 512
    #: Optional fixed value the caller cannot override (e.g. a constant API version).
    constant: Annotated[str, StringConstraints(max_length=512)] | None = None


class RestOperationSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_type: Literal[OperationType.REST] = OperationType.REST
    method: HttpMethod
    #: Path template relative to the integration base URL, e.g. "/v1/contacts/{id}".
    path: Annotated[str, StringConstraints(min_length=1, max_length=1024, pattern=r"^/")]
    path_params: dict[ParamName, ParamSpec] = Field(default_factory=dict)
    query_params: dict[ParamName, ParamSpec] = Field(default_factory=dict)
    header_params: dict[HeaderName, ParamSpec] = Field(default_factory=dict)
    #: JSON Schema (draft 2020-12) for the request body, or None for no body.
    body_schema: dict[str, Any] | None = None
    success_status: tuple[Annotated[int, Field(ge=100, le=599)], ...] = (200, 201, 202, 204)
    response_content_type: Annotated[str, StringConstraints(max_length=128)] = "application/json"
    #: JSON Schema the decoded response must satisfy, or None to accept any JSON.
    response_schema: dict[str, Any] | None = None
    retry_class: RetryClass = RetryClass.NON_IDEMPOTENT
    timeout_seconds: Annotated[float, Field(gt=0, le=120)] | None = None

    @model_validator(mode="after")
    def _coherent(self) -> RestOperationSpec:
        for header in self.header_params:
            if header.lower() in RESERVED_REQUEST_HEADERS:
                raise ValueError(f"operation header {header!r} is reserved")
        template_params = _path_template_names(self.path)
        if template_params != set(self.path_params):
            raise ValueError("path template placeholders must match path_params exactly")
        if self.method is HttpMethod.GET and self.body_schema is not None:
            raise ValueError("a GET operation must not declare a body_schema")
        return self


class GraphQLOperationSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_type: Literal[OperationType.GRAPHQL] = OperationType.GRAPHQL
    #: The FIXED GraphQL document. Executed verbatim; the caller supplies only variables.
    document: Annotated[str, StringConstraints(min_length=1, max_length=20_000)]
    operation_name: Annotated[str, StringConstraints(max_length=128)] | None = None
    variables_schema: dict[str, Any] | None = None
    max_depth: Annotated[int, Field(ge=1, le=20)] = 8
    allow_mutation: bool = False
    response_schema: dict[str, Any] | None = None
    retry_class: RetryClass = RetryClass.NON_IDEMPOTENT
    timeout_seconds: Annotated[float, Field(gt=0, le=120)] | None = None


OperationSpec = RestOperationSpec | GraphQLOperationSpec


def _path_template_names(path: str) -> set[str]:
    names: set[str] = set()
    depth = 0
    current = ""
    for char in path:
        if char == "{":
            depth += 1
            if depth > 1:
                raise ValueError("nested path placeholders are not allowed")
            current = ""
        elif char == "}":
            if depth == 0:
                raise ValueError("unbalanced path placeholder")
            depth -= 1
            names.add(current)
        elif depth == 1:
            current += char
    if depth != 0:
        raise ValueError("unbalanced path placeholder")
    return names


# --- domain aggregates -------------------------------------------------------


class Integration(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    slug: str
    name: str
    description: str | None
    integration_type: IntegrationType
    status: IntegrationStatus
    base_url: str
    config_revision: int
    auth_profile: AuthProfile
    destination_rule: DestinationRule
    created_at: dt.datetime
    updated_at: dt.datetime

    def public_view(self) -> IntegrationView:
        return IntegrationView(
            id=self.id,
            organization_id=self.organization_id,
            slug=self.slug,
            name=self.name,
            description=self.description,
            integration_type=self.integration_type,
            status=self.status,
            base_url=self.base_url,
            config_revision=self.config_revision,
            auth_profile_type=self.auth_profile.profile_type,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class IntegrationOperation(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    integration_id: UUID
    operation_key: str
    operation_type: OperationType
    spec: OperationSpec = Field(discriminator="operation_type")
    config_revision: int
    created_at: dt.datetime
    updated_at: dt.datetime

    def public_view(self) -> IntegrationOperationView:
        return IntegrationOperationView(
            id=self.id,
            integration_id=self.integration_id,
            operation_key=self.operation_key,
            operation_type=self.operation_type,
            retry_class=self.spec.retry_class,
            config_revision=self.config_revision,
            created_at=self.created_at,
        )


class WebhookEndpoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    integration_id: UUID
    slug: str
    public_token: str
    event_type: str
    signature_scheme: WebhookSignatureScheme
    signature_header: str | None
    timestamp_header: str | None
    tolerance_seconds: int
    max_body_bytes: int
    credential_ref: str | None
    created_at: dt.datetime


# --- execution contract -----------------------------------------------------


class ExecutionRequest(BaseModel):
    """The one way to invoke an integration. No raw URL / method / headers / query."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    integration_id: UUID
    operation_key: OperationKey
    input: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: (
        Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{8,200}$")] | None
    ) = None
    #: Free-form correlation string echoed into the result and structured logs.
    correlation_id: Annotated[str, StringConstraints(max_length=128)] | None = None


class InvocationInput(BaseModel):
    """The validated, structured input a REST operation is built from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path_params: dict[str, Any] = Field(default_factory=dict)
    query_params: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, Any] = Field(default_factory=dict)
    body: Any | None = None


class IntegrationResult(BaseModel):
    """The normalized outcome of an execution. Returned for a success AND, where the Hub
    chooses to surface it rather than raise, for a handled failure."""

    model_config = ConfigDict(frozen=True)

    integration_id: UUID
    operation_key: str
    result_class: ResultClass
    ok: bool
    #: HTTP-ish status the caller should treat as canonical (upstream status for a
    #: successful passthrough, a synthetic status for a Hub-side rejection).
    status_code: int
    output: Any | None = None
    upstream_status: int | None = None
    error_code: str | None = None
    error_detail: str | None = None
    retry_count: int = 0
    duration_ms: int = 0
    config_revision: int = 0
    correlation_id: str | None = None
    idempotency_key: str | None = None
    replayed: bool = False


# --- API request / view models -------------------------------------------


class CreateIntegrationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    slug: Slug
    name: Name
    description: Description | None = None
    integration_type: IntegrationType
    base_url: Annotated[str, StringConstraints(min_length=1, max_length=2048)]
    auth_profile: AuthProfile = AuthProfile()
    destination_rule: DestinationRule = DestinationRule()


class UpdateIntegrationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Name | None = None
    description: Description | None = None
    base_url: Annotated[str, StringConstraints(min_length=1, max_length=2048)] | None = None
    auth_profile: AuthProfile | None = None
    destination_rule: DestinationRule | None = None


class SetOperationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_key: OperationKey
    spec: OperationSpec = Field(discriminator="operation_type")


class IntegrationView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    slug: str
    name: str
    description: str | None
    integration_type: IntegrationType
    status: IntegrationStatus
    base_url: str
    config_revision: int
    auth_profile_type: AuthProfileType
    created_at: dt.datetime
    updated_at: dt.datetime


class IntegrationOperationView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    integration_id: UUID
    operation_key: str
    operation_type: OperationType
    retry_class: RetryClass
    config_revision: int
    created_at: dt.datetime
