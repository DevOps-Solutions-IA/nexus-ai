"""Voice domain values and API contracts (NXS-P12).

Provider payloads are NEVER the canonical contract — a
:class:`~nexus_ai.voice.providers.base.VoiceProviderAdapter` normalizes them into the
shared objects below. Every request model is strict (``extra="forbid"``) and bounded.
Nothing here carries a provider API key, a signed WebSocket URL or audio.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from nexus_ai.voice.audio import AudioFormat, VoiceMediaDirection
from nexus_ai.voice.state_machine import VoiceSessionDisposition, VoiceSessionState

# --- bounded string aliases ------------------------------------------------------------

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{1,62}$")]
ExternalAccountId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
]
#: A provider voice / agent identifier. Bounded and opaque — never trusted just because
#: the provider accepts it; always resolved through an Organization-owned VoiceProfile.
ProviderVoiceRef = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
]
ProviderModelRef = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=96)
]
ProviderSessionId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
ProviderEventId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
IdempotencyKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{8,200}$")]
CorrelationId = Annotated[str, StringConstraints(max_length=128)]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)]

MAX_OPTION_KEYS = 16
MAX_OPTION_VALUE_LEN = 512
MAX_CONFIG_KEYS = 12
MAX_CONFIG_VALUE_LEN = 1_024


class VoiceProvider(StrEnum):
    ELEVENLABS = "elevenlabs"
    FAKE = "fake"


class VoiceAccountStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class VoiceProfileStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class VoiceSessionDirection(StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class VoiceHandoffState(StrEnum):
    """Controlled AI <-> human handoff (NXS-VOICE-001). Control-plane only: P12 records
    the intent and detaches the AI voice stream; NXS-P11 owns the actual call bridging."""

    AI = "AI"
    PENDING_HUMAN = "PENDING_HUMAN"
    HUMAN = "HUMAN"


class VoiceProviderEventKind(StrEnum):
    """Normalized provider real-time event kinds (distinct from the P04 outbox names)."""

    SESSION_STARTED = "SESSION_STARTED"
    SESSION_ENDED = "SESSION_ENDED"
    AUDIO_OUTPUT = "AUDIO_OUTPUT"
    TRANSCRIPT = "TRANSCRIPT"
    AGENT_TEXT = "AGENT_TEXT"
    INTERRUPTION = "INTERRUPTION"
    KEEPALIVE = "KEEPALIVE"
    ERROR = "ERROR"


class TranscriptRole(StrEnum):
    USER = "USER"
    AGENT = "AGENT"


# --- normalized shared domain objects ------------------------------------------------


class VoiceLatencyMetrics(BaseModel):
    """Bounded, provider-neutral timing. Never a secret, never transcript content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connect_ms: Annotated[int, Field(ge=0, le=600_000)] | None = None
    time_to_first_audio_ms: Annotated[int, Field(ge=0, le=600_000)] | None = None
    session_duration_ms: Annotated[int, Field(ge=0, le=86_400_000)] | None = None


class VoiceUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    audio_seconds_in: Annotated[float, Field(ge=0, le=86_400)] = 0.0
    audio_seconds_out: Annotated[float, Field(ge=0, le=86_400)] = 0.0
    provider_characters: Annotated[int, Field(ge=0, le=100_000_000)] = 0
    interruptions: Annotated[int, Field(ge=0, le=1_000_000)] = 0


class VoiceProviderAccount(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    provider: VoiceProvider
    slug: str
    external_account_id: str
    credential_ref: str | None
    status: VoiceAccountStatus
    configuration: dict[str, Any]
    webhook_token: str
    created_at: dt.datetime
    updated_at: dt.datetime

    def public_view(self) -> VoiceProviderAccountView:
        return VoiceProviderAccountView(
            id=self.id,
            organization_id=self.organization_id,
            provider=self.provider,
            slug=self.slug,
            external_account_id=self.external_account_id,
            status=self.status,
            has_credential=self.credential_ref is not None,
            configuration=self.configuration,
            receive_path=f"/api/v1/webhooks/voice/{self.provider.value}/{self.webhook_token}",
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class VoiceProfile(BaseModel):
    """An Organization-owned voice configuration resource. A caller references one of
    these by id — a raw provider ``voice_id`` / ``agent_id`` is never accepted."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    account_id: UUID
    slug: str
    display_name: str
    provider_voice_ref: str
    provider_model_ref: str | None
    status: VoiceProfileStatus
    input_format: AudioFormat
    output_format: AudioFormat
    config: dict[str, Any]
    created_at: dt.datetime
    updated_at: dt.datetime

    def public_view(self) -> VoiceProfileView:
        return VoiceProfileView(
            id=self.id,
            organization_id=self.organization_id,
            account_id=self.account_id,
            slug=self.slug,
            display_name=self.display_name,
            status=self.status,
            input_format=self.input_format,
            output_format=self.output_format,
            config=self.config,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class VoiceSession(BaseModel):
    """A normalized, persisted real-time voice session. Contains no provider credential,
    no signed WebSocket URL and no audio."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    account_id: UUID
    voice_profile_id: UUID | None
    call_id: UUID
    media_session_id: UUID
    direction: VoiceSessionDirection
    state: VoiceSessionState
    disposition: VoiceSessionDisposition | None
    state_rank: int
    handoff_state: VoiceHandoffState
    provider: VoiceProvider
    provider_session_id: str | None
    bridge_ref: str | None
    negotiated_format: AudioFormat | None
    idempotency_key: str | None
    request_fingerprint: str | None
    error_code: str | None
    latency: VoiceLatencyMetrics | None
    usage: VoiceUsage | None
    correlation_id: str | None
    provider_timestamp: dt.datetime | None
    provider_sequence: int | None
    connecting_at: dt.datetime | None
    connected_at: dt.datetime | None
    ended_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime

    def public_view(self) -> VoiceSessionView:
        return VoiceSessionView(
            id=self.id,
            organization_id=self.organization_id,
            account_id=self.account_id,
            voice_profile_id=self.voice_profile_id,
            call_id=self.call_id,
            media_session_id=self.media_session_id,
            direction=self.direction,
            state=self.state,
            disposition=self.disposition,
            handoff_state=self.handoff_state,
            provider=self.provider,
            provider_session_id=self.provider_session_id,
            negotiated_format=self.negotiated_format,
            error_code=self.error_code,
            latency=self.latency,
            usage=self.usage,
            correlation_id=self.correlation_id,
            connecting_at=self.connecting_at,
            connected_at=self.connected_at,
            ended_at=self.ended_at,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class VoiceProviderEvent(BaseModel):
    """The output of ``VoiceProviderAdapter.parse_frame`` — one provider-neutral fact
    about a session. Transcript text is a transport-level fact only; P12 does not reason
    over it (that is NXS-P13)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: VoiceProviderEventKind
    provider_event_id: str | None = None
    provider_sequence: Annotated[int, Field(ge=0)] | None = None
    provider_timestamp: dt.datetime | None = None
    #: AUDIO_OUTPUT — decoded audio payload (bytes), bounded by the runtime before use.
    audio: bytes | None = None
    #: TRANSCRIPT / AGENT_TEXT — bounded text, never persisted by default.
    text: Annotated[str, StringConstraints(max_length=8_192)] | None = None
    role: TranscriptRole | None = None
    is_final: bool | None = None
    #: ERROR — a safe provider error label, never a raw body.
    error_label: Annotated[str, StringConstraints(max_length=200)] | None = None
    #: SESSION_STARTED — the provider's session id, if it supplies one.
    provider_session_id: str | None = None

    @model_validator(mode="after")
    def _coherent(self) -> VoiceProviderEvent:
        if self.kind is VoiceProviderEventKind.AUDIO_OUTPUT and self.audio is None:
            raise ValueError("an AUDIO_OUTPUT event requires audio")
        if (
            self.kind in (VoiceProviderEventKind.TRANSCRIPT, VoiceProviderEventKind.AGENT_TEXT)
            and self.text is None
        ):
            raise ValueError("a TRANSCRIPT / AGENT_TEXT event requires text")
        return self


class VoiceSessionContext(BaseModel):
    """The trusted, server-computed context a session runs under. Never caller-supplied."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    organization_id: UUID
    session_id: UUID
    call_id: UUID
    media_session_id: UUID
    account_id: UUID
    direction: VoiceSessionDirection
    input_format: AudioFormat
    output_format: AudioFormat
    correlation_id: str | None = None


# --- API contracts -------------------------------------------------------------------


class CreateVoiceAccountRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: VoiceProvider
    slug: Slug
    external_account_id: ExternalAccountId
    configuration: dict[str, Any] = Field(default_factory=dict)


class UpdateVoiceAccountRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    configuration: dict[str, Any] | None = None


class StoreVoiceCredentialRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Opaque provider-secret fields (e.g. {"api_key": "...", "webhook_secret": "..."}).
    #: Bounded; stored only in the vault; never returned, logged or evented.
    fields: dict[str, Annotated[str, StringConstraints(min_length=1, max_length=8_192)]]

    @model_validator(mode="after")
    def _bounded(self) -> StoreVoiceCredentialRequest:
        if not self.fields or len(self.fields) > 8:
            raise ValueError("between 1 and 8 credential fields are required")
        for name in self.fields:
            if not name.replace("_", "a").isalnum() or not name[0].isalpha():
                raise ValueError(f"credential field name {name!r} is not a valid identifier")
        return self


class _AudioFormatInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    codec: Annotated[str, StringConstraints(strip_whitespace=True, max_length=32)]
    sample_rate: Annotated[int, Field(ge=8_000, le=48_000)]
    channels: Annotated[int, Field(ge=1, le=1)] = 1
    frame_ms: Annotated[int, Field(ge=10, le=60)] = 20


class CreateVoiceProfileRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: UUID
    slug: Slug
    display_name: DisplayName
    provider_voice_ref: ProviderVoiceRef
    provider_model_ref: ProviderModelRef | None = None
    input_format: _AudioFormatInput
    output_format: _AudioFormatInput
    config: dict[str, Annotated[str, StringConstraints(max_length=MAX_CONFIG_VALUE_LEN)]] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def _bounded_config(self) -> CreateVoiceProfileRequest:
        if len(self.config) > MAX_CONFIG_KEYS:
            raise ValueError(f"at most {MAX_CONFIG_KEYS} config keys are allowed")
        for key in self.config:
            if not (1 <= len(key) <= 64) or not key.replace("_", "a").replace("-", "a").isalnum():
                raise ValueError(f"config key {key!r} is not a valid identifier")
        return self


class UpdateVoiceProfileRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    display_name: DisplayName | None = None
    provider_voice_ref: ProviderVoiceRef | None = None
    provider_model_ref: ProviderModelRef | None = None
    config: dict[str, Annotated[str, StringConstraints(max_length=MAX_CONFIG_VALUE_LEN)]] | None = (
        None
    )


#: The ONLY provider-neutral session-option keys a caller may set. Deliberately excludes
#: anything that could select a provider endpoint / URL / host / credential.
ALLOWED_SESSION_OPTION_KEYS: frozenset[str] = frozenset(
    {"language", "first_message", "greeting", "prompt_variant", "max_duration_hint"}
)


class StartVoiceSessionRequest(BaseModel):
    """The one governed way to attach a real-time voice stream to an ACTIVE NXS-P11 media
    session. No raw provider config, no WebSocket URL, no API key, no ARI command, and
    NO endpoint selection — ``options`` keys are a fixed allow-list
    (:data:`ALLOWED_SESSION_OPTION_KEYS`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: UUID
    media_session_id: UUID
    provider_account_id: UUID
    voice_profile_id: UUID
    correlation_id: CorrelationId | None = None
    idempotency_key: IdempotencyKey | None = None
    options: dict[str, Annotated[str, StringConstraints(max_length=MAX_OPTION_VALUE_LEN)]] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def _bounded_options(self) -> StartVoiceSessionRequest:
        if len(self.options) > MAX_OPTION_KEYS:
            raise ValueError(f"at most {MAX_OPTION_KEYS} options are allowed")
        for key in self.options:
            if key not in ALLOWED_SESSION_OPTION_KEYS:
                raise ValueError(
                    f"session option {key!r} is not permitted "
                    f"(allowed: {sorted(ALLOWED_SESSION_OPTION_KEYS)})"
                )
        return self


class StopVoiceSessionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: Annotated[str, StringConstraints(max_length=64)] | None = None


class RequestHandoffRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Where the call goes after the AI voice detaches. Bounded label only — P11 / a
    #: future phase performs the actual bridge.
    target: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{1,63}$")] = "human_agent"
    note: Annotated[str, StringConstraints(max_length=200)] | None = None


class ConfirmHandoffRequest(BaseModel):
    """The argument object for the NON-PUBLIC in-process seam
    :meth:`~nexus_ai.voice.service.VoiceService.confirm_handoff` — NOT a public API
    request. P12 wires no route to it. The future NXS-P17 (Human Agent Operations)
    service constructs it AFTER it has authoritatively verified the human bridge, and
    ``bridge_reference`` is the reference that authority has already validated (P12 only
    records it). It is defined here so the seam contract is frozen for NXS-P17."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{1,63}$")] = "human_agent"
    #: The bridge reference the trusted authority (NXS-P17) has already verified.
    bridge_reference: Annotated[str, StringConstraints(min_length=1, max_length=200)]


# --- views -------------------------------------------------------------------------


class VoiceProviderAccountView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    provider: VoiceProvider
    slug: str
    external_account_id: str
    status: VoiceAccountStatus
    has_credential: bool
    configuration: dict[str, Any]
    receive_path: str
    created_at: dt.datetime
    updated_at: dt.datetime


class VoiceProfileView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    account_id: UUID
    slug: str
    display_name: str
    status: VoiceProfileStatus
    input_format: AudioFormat
    output_format: AudioFormat
    config: dict[str, Any]
    created_at: dt.datetime
    updated_at: dt.datetime


class VoiceSessionView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    account_id: UUID
    voice_profile_id: UUID | None
    call_id: UUID
    media_session_id: UUID
    direction: VoiceSessionDirection
    state: VoiceSessionState
    disposition: VoiceSessionDisposition | None
    handoff_state: VoiceHandoffState
    provider: VoiceProvider
    provider_session_id: str | None
    negotiated_format: AudioFormat | None
    error_code: str | None
    latency: VoiceLatencyMetrics | None
    usage: VoiceUsage | None
    correlation_id: str | None
    connecting_at: dt.datetime | None
    connected_at: dt.datetime | None
    ended_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime


class VoiceUsageView(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: UUID
    usage: VoiceUsage
    latency: VoiceLatencyMetrics
    recorded_at: dt.datetime


MEDIA_DIRECTION_FOR = {
    VoiceSessionDirection.INBOUND: VoiceMediaDirection.BIDIRECTIONAL,
    VoiceSessionDirection.OUTBOUND: VoiceMediaDirection.BIDIRECTIONAL,
}
