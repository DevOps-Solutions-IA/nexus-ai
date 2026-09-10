"""Telephony domain values and API contracts (NXS-P11).

Provider payloads are NEVER the canonical contract — a :class:`TelephonyProviderAdapter`
normalizes them into the shared objects below. Every request model is strict
(``extra="forbid"``) and bounded. Nothing here carries a SIP password, an ARI credential
or a provider token.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

# --- bounded string aliases ---------------------------------------------------------

ProviderKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,47}$")]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{1,62}$")]
ExternalAccountId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
]
ProviderCallId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
ProviderEventId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
IdempotencyKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{8,200}$")]
CorrelationId = Annotated[str, StringConstraints(max_length=128)]
#: A destination the caller supplies: E.164 phone or (internal) sip: endpoint alias.
DestinationInput = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=3, max_length=128)
]
E164 = Annotated[str, StringConstraints(pattern=r"^\+[1-9][0-9]{6,14}$")]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)]

MAX_METADATA_KEYS = 16
MAX_METADATA_VALUE_LEN = 256


class TelephonyProvider(StrEnum):
    """Registered provider families. Provider-specific behaviour lives behind an adapter;
    ``ASTERISK`` is the Nexus telephony engine (ARI control boundary)."""

    ASTERISK = "asterisk"
    FAKE = "fake"


class CallDirection(StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class CallState(StrEnum):
    #: Canonical, monotonic call lifecycle. A provider callback is mapped to one of these
    #: by the adapter; the state machine enforces the legal transitions.
    CREATED = "CREATED"
    RINGING = "RINGING"
    EARLY_MEDIA = "EARLY_MEDIA"
    ANSWERED = "ANSWERED"
    BRIDGED = "BRIDGED"
    ENDING = "ENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BUSY = "BUSY"
    NO_ANSWER = "NO_ANSWER"


#: Terminal states — no further transition is legal.
TERMINAL_CALL_STATES = frozenset(
    {
        CallState.COMPLETED,
        CallState.FAILED,
        CallState.CANCELLED,
        CallState.BUSY,
        CallState.NO_ANSWER,
    }
)


class CallDisposition(StrEnum):
    #: Why a call ended. Set on the terminal transition; never changed afterwards.
    ANSWERED = "ANSWERED"
    NO_ANSWER = "NO_ANSWER"
    BUSY = "BUSY"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class CallLegRole(StrEnum):
    #: The A-leg is the originating party; the B-leg is the far end being reached.
    A_LEG = "A_LEG"
    B_LEG = "B_LEG"


class ParticipantKind(StrEnum):
    PSTN = "PSTN"
    SIP_ENDPOINT = "SIP_ENDPOINT"
    UNKNOWN = "UNKNOWN"


class MediaDirection(StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"
    BIDIRECTIONAL = "BIDIRECTIONAL"


class MediaState(StrEnum):
    #: Foundation-only media lifecycle. NXS-P12 attaches an external voice stream to an
    #: ACTIVE session; P11 never carries audio.
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    STOPPED = "STOPPED"


TERMINAL_MEDIA_STATES = frozenset({MediaState.STOPPED})


class DtmfDigit(StrEnum):
    D0 = "0"
    D1 = "1"
    D2 = "2"
    D3 = "3"
    D4 = "4"
    D5 = "5"
    D6 = "6"
    D7 = "7"
    D8 = "8"
    D9 = "9"
    STAR = "*"
    HASH = "#"
    A = "A"
    B = "B"
    C = "C"
    D = "D"


class TelephonyEventType(StrEnum):
    #: Normalized provider-event kinds (distinct from the P04 outbox event names).
    STATE = "STATE"
    DTMF = "DTMF"
    MEDIA = "MEDIA"


class AccountStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


# --- normalized shared domain objects --------------------------------------------


class CallParticipant(BaseModel):
    """A normalized endpoint on one call leg. A phone participant is canonical E.164; a
    SIP participant is a bounded, control-char-free alias — never a raw sip: URI."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ParticipantKind
    address: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    display_name: DisplayName | None = None


class CallLeg(BaseModel):
    """One leg of a call. The A-leg is the originator; the B-leg is the far end. Legs are
    stored on the call as a bounded JSON array — NXS-P12 promotes them to their own
    table only if it needs per-leg media rows (documented in ADR-0086)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: CallLegRole
    participant: CallParticipant
    provider_leg_id: Annotated[str, StringConstraints(max_length=200)] | None = None
    sip_call_id: Annotated[str, StringConstraints(max_length=200)] | None = None


class RoutingContext(BaseModel):
    """How an inbound call was routed to an Organization: which owned number the provider
    dialed, and the resolved account. Computed at ingest, never caller-supplied."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    organization_id: UUID
    account_id: UUID
    phone_number_id: UUID
    dialed_number: E164


class MediaCodecInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,32}$")]
    clock_rate: Annotated[int, Field(ge=8000, le=192_000)] | None = None
    channels: Annotated[int, Field(ge=1, le=2)] | None = None


class MediaSession(BaseModel):
    """The foundation P12 attaches an external voice stream to. P11 records identifiers
    and lifecycle only — no audio, no recording."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    call_id: UUID
    direction: MediaDirection
    state: MediaState
    bridge_id: str | None
    stream_id: str | None
    codec: MediaCodecInfo | None
    started_at: dt.datetime | None
    stopped_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime


class TelephonyAccount(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    provider: TelephonyProvider
    slug: str
    external_account_id: str
    credential_ref: str | None
    status: AccountStatus
    configuration: dict[str, Any]
    webhook_token: str
    created_at: dt.datetime
    updated_at: dt.datetime

    def public_view(self) -> TelephonyAccountView:
        return TelephonyAccountView(
            id=self.id,
            organization_id=self.organization_id,
            provider=self.provider,
            slug=self.slug,
            external_account_id=self.external_account_id,
            status=self.status,
            has_credential=self.credential_ref is not None,
            configuration=self.configuration,
            receive_path=f"/api/v1/webhooks/telephony/{self.provider.value}/{self.webhook_token}",
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class PhoneNumber(BaseModel):
    """An Organization-owned phone-number resource. Outbound caller ID references one of
    these by id — a raw ``from`` string is never accepted (caller-ID governance)."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    account_id: UUID
    e164: str
    display_name: str | None
    verified: bool
    inbound_enabled: bool
    created_at: dt.datetime
    updated_at: dt.datetime

    def public_view(self) -> PhoneNumberView:
        return PhoneNumberView(**self.model_dump())


class Call(BaseModel):
    """A normalized, persisted call. Contains no SIP credential and no raw SDP."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    account_id: UUID
    from_number_id: UUID | None
    direction: CallDirection
    state: CallState
    disposition: CallDisposition | None
    state_rank: int
    provider: TelephonyProvider
    provider_call_id: str | None
    from_address: str
    to_address: str
    legs: tuple[CallLeg, ...]
    correlation_id: str | None
    idempotency_key: str | None
    error_code: str | None
    provider_timestamp: dt.datetime | None
    ringing_at: dt.datetime | None
    answered_at: dt.datetime | None
    ended_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime

    def public_view(self) -> CallView:
        return CallView(
            id=self.id,
            organization_id=self.organization_id,
            account_id=self.account_id,
            from_number_id=self.from_number_id,
            direction=self.direction,
            state=self.state,
            disposition=self.disposition,
            provider=self.provider,
            provider_call_id=self.provider_call_id,
            from_address=self.from_address,
            to_address=self.to_address,
            legs=self.legs,
            correlation_id=self.correlation_id,
            error_code=self.error_code,
            ringing_at=self.ringing_at,
            answered_at=self.answered_at,
            ended_at=self.ended_at,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


# --- normalized provider events --------------------------------------------------


class NormalizedCallEvent(BaseModel):
    """The output of ``TelephonyProviderAdapter.normalize_provider_event`` — a single,
    provider-neutral fact about a call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_event_id: str
    provider_call_id: str
    event_type: TelephonyEventType
    #: For STATE events.
    state: CallState | None = None
    disposition: CallDisposition | None = None
    #: For DTMF events.
    digit: DtmfDigit | None = None
    #: For MEDIA events.
    media_state: MediaState | None = None
    media_direction: MediaDirection | None = None
    bridge_id: Annotated[str, StringConstraints(max_length=200)] | None = None
    stream_id: Annotated[str, StringConstraints(max_length=200)] | None = None
    #: Provider ordering hints.
    provider_timestamp: dt.datetime | None = None
    provider_sequence: Annotated[int, Field(ge=0)] | None = None
    #: Bounded, redacted caller/callee hints for leg construction.
    from_participant: CallParticipant | None = None
    to_participant: CallParticipant | None = None

    @model_validator(mode="after")
    def _coherent(self) -> NormalizedCallEvent:
        if self.event_type is TelephonyEventType.STATE and self.state is None:
            raise ValueError("a STATE event requires a state")
        if self.event_type is TelephonyEventType.DTMF and self.digit is None:
            raise ValueError("a DTMF event requires a digit")
        if self.event_type is TelephonyEventType.MEDIA and self.media_state is None:
            raise ValueError("a MEDIA event requires a media_state")
        return self


class NormalizedInboundCall(BaseModel):
    """A provider ``call.created`` / first-seen inbound event, normalized."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_event_id: str
    provider_call_id: str
    dialed_number: E164
    caller: CallParticipant
    provider_timestamp: dt.datetime | None = None
    correlation_id: CorrelationId | None = None


# --- API contracts -------------------------------------------------------------


class CreateAccountRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: TelephonyProvider
    slug: Slug
    external_account_id: ExternalAccountId
    configuration: dict[str, Any] = Field(default_factory=dict)


class UpdateAccountRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    configuration: dict[str, Any] | None = None


class StoreAccountCredentialRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Opaque provider-secret fields (e.g. {"ari_user": "...", "ari_password": "...",
    #: "webhook_secret": "..."}). Bounded; stored only in the vault; never returned.
    fields: dict[str, Annotated[str, StringConstraints(min_length=1, max_length=4096)]]

    @model_validator(mode="after")
    def _bounded(self) -> StoreAccountCredentialRequest:
        if not self.fields or len(self.fields) > 8:
            raise ValueError("between 1 and 8 credential fields are required")
        for name in self.fields:
            if not name.replace("_", "a").isalnum() or not name[0].isalpha():
                raise ValueError(f"credential field name {name!r} is not a valid identifier")
        return self


class RegisterPhoneNumberRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: UUID
    e164: E164
    display_name: DisplayName | None = None
    inbound_enabled: bool = True


class CreateCallRequest(BaseModel):
    """The one governed way to place a call. The caller names an account and an
    Organization-owned ``from_number_id`` (never a raw ``from`` string), and a bounded
    destination. No raw SIP header, dialplan, ARI/AMI action or shell surface."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_account_id: UUID
    from_number_id: UUID
    destination: DestinationInput
    correlation_id: CorrelationId | None = None
    idempotency_key: IdempotencyKey | None = None
    metadata: dict[str, Annotated[str, StringConstraints(max_length=MAX_METADATA_VALUE_LEN)]] = (
        Field(default_factory=dict)
    )

    @model_validator(mode="after")
    def _bounded_metadata(self) -> CreateCallRequest:
        if len(self.metadata) > MAX_METADATA_KEYS:
            raise ValueError(f"at most {MAX_METADATA_KEYS} metadata keys are allowed")
        for key in self.metadata:
            if not (1 <= len(key) <= 64) or not key.replace("_", "a").replace("-", "a").isalnum():
                raise ValueError(f"metadata key {key!r} is not a valid identifier")
        return self


class SendDtmfRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    digits: Annotated[str, StringConstraints(min_length=1, max_length=128)]

    @model_validator(mode="after")
    def _digits_only(self) -> SendDtmfRequest:
        allowed = set("0123456789*#ABCD")
        if any(ch not in allowed for ch in self.digits):
            raise ValueError("DTMF digits must be 0-9, * , # or A-D")
        return self


class HangupCallRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: Annotated[str, StringConstraints(max_length=64)] | None = None


# --- views -------------------------------------------------------------------------


class TelephonyAccountView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    provider: TelephonyProvider
    slug: str
    external_account_id: str
    status: AccountStatus
    has_credential: bool
    configuration: dict[str, Any]
    receive_path: str
    created_at: dt.datetime
    updated_at: dt.datetime


class PhoneNumberView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    account_id: UUID
    e164: str
    display_name: str | None
    verified: bool
    inbound_enabled: bool
    created_at: dt.datetime
    updated_at: dt.datetime


class CallView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    account_id: UUID
    from_number_id: UUID | None
    direction: CallDirection
    state: CallState
    disposition: CallDisposition | None
    provider: TelephonyProvider
    provider_call_id: str | None
    from_address: str
    to_address: str
    legs: tuple[CallLeg, ...]
    correlation_id: str | None
    error_code: str | None
    ringing_at: dt.datetime | None
    answered_at: dt.datetime | None
    ended_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime


class DtmfResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    call_id: UUID
    digits: str
    accepted: bool
