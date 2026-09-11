"""AI Agent Runtime domain values and API contracts (NXS-P13: NXS-AGENT-001).

Model provider payloads are NEVER the canonical contract — a
:class:`~nexus_ai.agents.models.base.ModelProviderAdapter` normalizes them. Every request
model is strict (``extra="forbid"``) and every string / list / map is bounded. Nothing
here carries a model API key, a raw endpoint, a raw provider payload, a raw prompt or any
model reasoning. The runtime persists externally-meaningful execution facts only —
there is no chain-of-thought field anywhere.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from nexus_ai.agents.state_machine import (
    AgentSessionDisposition,
    AgentSessionState,
    AgentTurnState,
)

# --- bounded string aliases ----------------------------------------------------------

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{1,62}$")]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
ExternalAccountId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
]
#: An opaque provider model identifier (e.g. "gpt-4o-mini"). Bounded; never trusted
#: because a provider accepts it — always resolved through an Organization-owned profile.
ProviderModelRef = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=96)
]
IdempotencyKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{8,200}$")]
CorrelationId = Annotated[str, StringConstraints(max_length=128)]
ToolKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{1,94}$")]
#: A bare https origin — never a per-call URL. SSRF-validated at registration.
ApiBase = Annotated[
    str, StringConstraints(pattern=r"^https://[a-z0-9.-]+(?::[0-9]{1,5})?(?:/[a-z0-9._~/-]*)?$")
]

MAX_TOOL_KEYS = 32
MAX_CONFIG_KEYS = 12

#: The ONLY provider-neutral session-metadata keys a caller may set. Deliberately
#: excludes anything that could select a model, an endpoint, a credential or an
#: instruction layer.
ALLOWED_SESSION_METADATA_KEYS: frozenset[str] = frozenset(
    {"language", "channel_ref", "locale", "purpose"}
)


class ModelProvider(StrEnum):
    OPENAI_COMPATIBLE = "openai_compatible"
    FAKE = "fake"


class ModelProviderAccountStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class ModelProfileStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class AgentStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class AgentChannel(StrEnum):
    """Where a turn's user content originates. Channel-neutral — the runtime treats every
    channel identically and the delivery of a response stays a channel-service concern."""

    API = "API"
    VOICE = "VOICE"
    WHATSAPP = "WHATSAPP"
    EMAIL = "EMAIL"
    SMS = "SMS"


class AgentTurnKind(StrEnum):
    """The kind of externally-meaningful execution fact a turn record captures. There is
    deliberately no MODEL_REQUEST body and no reasoning trace."""

    USER_INPUT = "USER_INPUT"
    MODEL_OUTPUT = "MODEL_OUTPUT"
    TOOL_RESULT = "TOOL_RESULT"
    FINAL_RESPONSE = "FINAL_RESPONSE"
    ERROR = "ERROR"


class AgentToolCallStatus(StrEnum):
    REQUESTED = "REQUESTED"
    ALLOWED = "ALLOWED"
    DENIED = "DENIED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


# --- domain models -----------------------------------------------------------------


class ModelProviderAccount(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    provider: ModelProvider
    slug: str
    api_base: str
    external_account_id: str
    credential_ref: str | None
    status: ModelProviderAccountStatus
    configuration: dict[str, Any]
    created_at: dt.datetime
    updated_at: dt.datetime

    def public_view(self) -> ModelProviderAccountView:
        return ModelProviderAccountView(
            id=self.id,
            organization_id=self.organization_id,
            provider=self.provider,
            slug=self.slug,
            api_base=self.api_base,
            external_account_id=self.external_account_id,
            status=self.status,
            configuration=self.configuration,
            has_credential=self.credential_ref is not None,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class ModelProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    account_id: UUID
    slug: str
    display_name: str
    model: str
    temperature: float | None
    max_output_tokens: int | None
    status: ModelProfileStatus
    created_at: dt.datetime
    updated_at: dt.datetime

    def public_view(self) -> ModelProfileView:
        return ModelProfileView(**self.model_dump())


class AgentDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    slug: str
    display_name: str
    status: AgentStatus
    model_profile_id: UUID
    system_instructions: str
    tool_keys: tuple[str, ...]
    max_tool_iterations: int
    max_output_tokens: int | None
    temperature: float | None
    timeout_seconds: float | None
    created_at: dt.datetime
    updated_at: dt.datetime

    def public_view(self) -> AgentView:
        return AgentView(**self.model_dump())


class AgentSession(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    agent_id: UUID
    model_profile_id: UUID
    state: AgentSessionState
    disposition: AgentSessionDisposition | None
    channel: AgentChannel
    initiator_user_id: UUID
    initiator_session_id: UUID
    conversation_id: UUID | None
    customer_id: UUID | None
    call_id: UUID | None
    voice_session_id: UUID | None
    correlation_id: str | None
    idempotency_key: str | None
    request_fingerprint: str | None
    turn_count: int
    input_tokens: int
    output_tokens: int
    tool_call_count: int
    error_code: str | None
    started_at: dt.datetime | None
    last_activity_at: dt.datetime | None
    ended_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime

    def public_view(self) -> AgentSessionView:
        return AgentSessionView(
            id=self.id,
            organization_id=self.organization_id,
            agent_id=self.agent_id,
            model_profile_id=self.model_profile_id,
            state=self.state,
            disposition=self.disposition,
            channel=self.channel,
            conversation_id=self.conversation_id,
            customer_id=self.customer_id,
            call_id=self.call_id,
            voice_session_id=self.voice_session_id,
            correlation_id=self.correlation_id,
            turn_count=self.turn_count,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            total_tokens=self.input_tokens + self.output_tokens,
            tool_call_count=self.tool_call_count,
            error_code=self.error_code,
            started_at=self.started_at,
            last_activity_at=self.last_activity_at,
            ended_at=self.ended_at,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class AgentTurn(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    session_id: UUID
    sequence: int
    state: AgentTurnState
    channel: AgentChannel
    #: the user's message and the assistant's final answer — the CONVERSATION, bounded.
    #: NOT reasoning: there is no scratchpad / chain-of-thought field.
    input_text: str
    response_text: str | None
    input_char_count: int
    response_char_count: int
    model: str | None
    finish_reason: str | None
    input_tokens: int
    output_tokens: int
    tool_iterations: int
    #: DISTINCT tool calls actually executed — NOT tool_iterations. The fact an exact
    #: replay reconstructs AgentResponse.tool_calls from (audit corrective #5).
    tool_call_count: int
    latency_ms: int
    error_code: str | None
    idempotency_key: str | None
    request_fingerprint: str | None
    #: the correlation id the ORIGINAL response published under — replayed verbatim.
    response_correlation_id: str | None
    created_at: dt.datetime
    updated_at: dt.datetime

    def public_view(self) -> AgentTurnView:
        return AgentTurnView(
            id=self.id,
            session_id=self.session_id,
            sequence=self.sequence,
            state=self.state,
            channel=self.channel,
            input_char_count=self.input_char_count,
            response_char_count=self.response_char_count,
            model=self.model,
            finish_reason=self.finish_reason,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            total_tokens=self.input_tokens + self.output_tokens,
            tool_iterations=self.tool_iterations,
            tool_call_count=self.tool_call_count,
            latency_ms=self.latency_ms,
            error_code=self.error_code,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class AgentToolCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    session_id: UUID
    turn_id: UUID
    sequence: int
    iteration: int
    tool_key: str
    arguments_hash: str
    status: AgentToolCallStatus
    tool_result_class: str | None
    tool_status_code: int | None
    denied_reason: str | None
    latency_ms: int
    created_at: dt.datetime


# --- channel-neutral runtime values ------------------------------------------------


class AgentInput(BaseModel):
    """The channel-neutral input to one agent turn."""

    model_config = ConfigDict(frozen=True)

    content: str
    channel: AgentChannel
    correlation_id: str | None = None


class AgentResponse(BaseModel):
    """The channel-neutral output of one agent turn. Delivery to a channel is a
    downstream concern — P13 produces text + safe execution facts only."""

    model_config = ConfigDict(frozen=True)

    session_id: UUID
    turn_id: UUID
    content: str
    finish_reason: str
    tool_calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    latency_ms: int
    correlation_id: str | None = None


# --- API request contracts --------------------------------------------------------


class RegisterModelProviderAccountRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: ModelProvider
    slug: Slug
    external_account_id: ExternalAccountId
    #: A bare https origin for the provider REST API — SSRF-validated. Never a per-call URL.
    api_base: ApiBase
    configuration: dict[str, Annotated[str, StringConstraints(max_length=1024)]] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def _bounded_configuration(self) -> RegisterModelProviderAccountRequest:
        if len(self.configuration) > MAX_CONFIG_KEYS:
            raise ValueError(f"at most {MAX_CONFIG_KEYS} configuration keys are allowed")
        return self


class UpdateModelProviderAccountRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    api_base: ApiBase | None = None
    configuration: dict[str, Annotated[str, StringConstraints(max_length=1024)]] | None = None
    status: ModelProviderAccountStatus | None = None


class StoreModelCredentialRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fields: dict[
        Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,63}$")],
        Annotated[str, StringConstraints(min_length=1, max_length=8192)],
    ]

    @model_validator(mode="after")
    def _bounded_fields(self) -> StoreModelCredentialRequest:
        if not (1 <= len(self.fields) <= 8):
            raise ValueError("between 1 and 8 credential fields are allowed")
        return self


class CreateModelProfileRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: UUID
    slug: Slug
    display_name: DisplayName
    model: ProviderModelRef
    temperature: Annotated[float, Field(ge=0.0, le=2.0)] | None = None
    max_output_tokens: Annotated[int, Field(ge=1, le=32_768)] | None = None


class UpdateModelProfileRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    display_name: DisplayName | None = None
    model: ProviderModelRef | None = None
    temperature: Annotated[float, Field(ge=0.0, le=2.0)] | None = None
    max_output_tokens: Annotated[int, Field(ge=1, le=32_768)] | None = None
    status: ModelProfileStatus | None = None


class CreateAgentRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    slug: Slug
    display_name: DisplayName
    model_profile_id: UUID
    #: Organization-owned trusted instruction layer. Bounded. This is NOT a raw system
    #: prompt from a runtime caller — it is a configuration resource an admin sets.
    system_instructions: Annotated[str, StringConstraints(min_length=1, max_length=16_384)]
    #: The agent's tool allow-list — a set of registered NXS-P08 ``tool_key``s. The model
    #: can only *request* a tool on this list; authority is still enforced by P08.
    tool_keys: tuple[ToolKey, ...] = ()
    max_tool_iterations: Annotated[int, Field(ge=0, le=32)] | None = None
    max_output_tokens: Annotated[int, Field(ge=1, le=32_768)] | None = None
    temperature: Annotated[float, Field(ge=0.0, le=2.0)] | None = None
    timeout_seconds: Annotated[float, Field(gt=0, le=600)] | None = None

    @model_validator(mode="after")
    def _bounded_tools(self) -> CreateAgentRequest:
        if len(self.tool_keys) > MAX_TOOL_KEYS:
            raise ValueError(f"at most {MAX_TOOL_KEYS} tools may be allow-listed")
        if len(set(self.tool_keys)) != len(self.tool_keys):
            raise ValueError("duplicate tool_key in the allow-list")
        return self


class UpdateAgentRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    display_name: DisplayName | None = None
    model_profile_id: UUID | None = None
    system_instructions: (
        Annotated[str, StringConstraints(min_length=1, max_length=16_384)] | None
    ) = None
    tool_keys: tuple[ToolKey, ...] | None = None
    max_tool_iterations: Annotated[int, Field(ge=0, le=32)] | None = None
    max_output_tokens: Annotated[int, Field(ge=1, le=32_768)] | None = None
    temperature: Annotated[float, Field(ge=0.0, le=2.0)] | None = None
    timeout_seconds: Annotated[float, Field(gt=0, le=600)] | None = None
    status: AgentStatus | None = None

    @model_validator(mode="after")
    def _bounded_tools(self) -> UpdateAgentRequest:
        if self.tool_keys is not None:
            if len(self.tool_keys) > MAX_TOOL_KEYS:
                raise ValueError(f"at most {MAX_TOOL_KEYS} tools may be allow-listed")
            if len(set(self.tool_keys)) != len(self.tool_keys):
                raise ValueError("duplicate tool_key in the allow-list")
        return self


class StartAgentSessionRequest(BaseModel):
    """The one governed way to open a controlled agent interaction context. No system
    prompt, no model selection, no endpoint, no credential — every one of those is a
    server-owned resource resolved from ``agent_id``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: UUID
    channel: AgentChannel = AgentChannel.API
    conversation_id: UUID | None = None
    customer_id: UUID | None = None
    call_id: UUID | None = None
    voice_session_id: UUID | None = None
    correlation_id: CorrelationId | None = None
    idempotency_key: IdempotencyKey | None = None
    metadata: dict[str, Annotated[str, StringConstraints(max_length=256)]] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def _bounded_metadata(self) -> StartAgentSessionRequest:
        for key in self.metadata:
            if key not in ALLOWED_SESSION_METADATA_KEYS:
                raise ValueError(
                    f"session metadata {key!r} is not permitted "
                    f"(allowed: {sorted(ALLOWED_SESSION_METADATA_KEYS)})"
                )
        return self


class SubmitTurnRequest(BaseModel):
    """One channel-neutral user turn. Its ``content`` is UNTRUSTED DATA — it can never
    modify Nexus permissions, reveal credentials, authorize a tool, override policy or
    bypass the Tool Engine."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: Annotated[str, StringConstraints(min_length=1, max_length=32_768)]
    idempotency_key: IdempotencyKey | None = None
    correlation_id: CorrelationId | None = None


class StopAgentSessionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: Annotated[str, StringConstraints(max_length=64)] | None = None


# --- views ---------------------------------------------------------------------------


class ModelProviderAccountView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    provider: ModelProvider
    slug: str
    api_base: str
    external_account_id: str
    status: ModelProviderAccountStatus
    configuration: dict[str, Any]
    has_credential: bool
    created_at: dt.datetime
    updated_at: dt.datetime


class ModelProfileView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    account_id: UUID
    slug: str
    display_name: str
    model: str
    temperature: float | None
    max_output_tokens: int | None
    status: ModelProfileStatus
    created_at: dt.datetime
    updated_at: dt.datetime


class AgentView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    slug: str
    display_name: str
    status: AgentStatus
    model_profile_id: UUID
    system_instructions: str
    tool_keys: tuple[str, ...]
    max_tool_iterations: int
    max_output_tokens: int | None
    temperature: float | None
    timeout_seconds: float | None
    created_at: dt.datetime
    updated_at: dt.datetime


class AgentSessionView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    agent_id: UUID
    model_profile_id: UUID
    state: AgentSessionState
    disposition: AgentSessionDisposition | None
    channel: AgentChannel
    conversation_id: UUID | None
    customer_id: UUID | None
    call_id: UUID | None
    voice_session_id: UUID | None
    correlation_id: str | None
    turn_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    tool_call_count: int
    error_code: str | None
    started_at: dt.datetime | None
    last_activity_at: dt.datetime | None
    ended_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime


class AgentTurnView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    session_id: UUID
    sequence: int
    state: AgentTurnState
    channel: AgentChannel
    input_char_count: int
    response_char_count: int
    model: str | None
    finish_reason: str | None
    input_tokens: int
    output_tokens: int
    total_tokens: int
    tool_iterations: int
    tool_call_count: int
    latency_ms: int
    error_code: str | None
    created_at: dt.datetime
    updated_at: dt.datetime
