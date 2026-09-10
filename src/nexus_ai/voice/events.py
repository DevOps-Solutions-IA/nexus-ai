"""Voice audit event payloads (NXS-P12 with NXS-EVENT-012).

Registered in the canonical NXS-P04 payload registry. Payloads carry IDs and safe
lifecycle / bounded-metric metadata ONLY — never a provider API key, a signed WebSocket
URL, an Authorization header, a raw provider frame, raw audio or transcript bodies. The
trusted ``organization_id`` comes from the envelope.
"""

from __future__ import annotations

from typing import ClassVar
from uuid import UUID

from nexus_ai.events.registry import EVENT_REGISTRY, EventPayload


class _VoiceSessionEventV1(EventPayload):
    session_id: UUID
    account_id: UUID
    call_id: UUID
    media_session_id: UUID
    direction: str
    provider: str
    state: str
    correlation_id: str | None = None


@EVENT_REGISTRY.payload_model("voice.session.created")
class VoiceSessionCreatedV1(_VoiceSessionEventV1):
    EVENT_TYPE: ClassVar[str] = "voice.session.created"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("voice.session.connecting")
class VoiceSessionConnectingV1(_VoiceSessionEventV1):
    EVENT_TYPE: ClassVar[str] = "voice.session.connecting"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("voice.session.connected")
class VoiceSessionConnectedV1(_VoiceSessionEventV1):
    EVENT_TYPE: ClassVar[str] = "voice.session.connected"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("voice.session.streaming")
class VoiceSessionStreamingV1(_VoiceSessionEventV1):
    EVENT_TYPE: ClassVar[str] = "voice.session.streaming"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("voice.session.ending")
class VoiceSessionEndingV1(_VoiceSessionEventV1):
    EVENT_TYPE: ClassVar[str] = "voice.session.ending"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("voice.session.completed")
class VoiceSessionCompletedV1(_VoiceSessionEventV1):
    EVENT_TYPE: ClassVar[str] = "voice.session.completed"
    VERSION: ClassVar[int] = 1

    disposition: str


@EVENT_REGISTRY.payload_model("voice.session.failed")
class VoiceSessionFailedV1(_VoiceSessionEventV1):
    EVENT_TYPE: ClassVar[str] = "voice.session.failed"
    VERSION: ClassVar[int] = 1

    disposition: str
    error_code: str | None = None


@EVENT_REGISTRY.payload_model("voice.session.cancelled")
class VoiceSessionCancelledV1(_VoiceSessionEventV1):
    EVENT_TYPE: ClassVar[str] = "voice.session.cancelled"
    VERSION: ClassVar[int] = 1

    disposition: str


@EVENT_REGISTRY.payload_model("voice.provider.connected")
class VoiceProviderConnectedV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "voice.provider.connected"
    VERSION: ClassVar[int] = 1

    session_id: UUID
    account_id: UUID
    provider: str
    correlation_id: str | None = None


@EVENT_REGISTRY.payload_model("voice.provider.disconnected")
class VoiceProviderDisconnectedV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "voice.provider.disconnected"
    VERSION: ClassVar[int] = 1

    session_id: UUID
    account_id: UUID
    provider: str
    reason: str | None = None
    correlation_id: str | None = None


@EVENT_REGISTRY.payload_model("voice.transcript.partial")
class VoiceTranscriptPartialV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "voice.transcript.partial"
    VERSION: ClassVar[int] = 1

    session_id: UUID
    call_id: UUID
    role: str
    #: A bounded character count only — the transcript body is NEVER carried.
    char_count: int
    correlation_id: str | None = None


@EVENT_REGISTRY.payload_model("voice.transcript.final")
class VoiceTranscriptFinalV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "voice.transcript.final"
    VERSION: ClassVar[int] = 1

    session_id: UUID
    call_id: UUID
    role: str
    char_count: int
    correlation_id: str | None = None


@EVENT_REGISTRY.payload_model("voice.interruption.started")
class VoiceInterruptionStartedV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "voice.interruption.started"
    VERSION: ClassVar[int] = 1

    session_id: UUID
    call_id: UUID
    correlation_id: str | None = None


@EVENT_REGISTRY.payload_model("voice.interruption.completed")
class VoiceInterruptionCompletedV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "voice.interruption.completed"
    VERSION: ClassVar[int] = 1

    session_id: UUID
    call_id: UUID
    correlation_id: str | None = None


@EVENT_REGISTRY.payload_model("voice.handoff.requested")
class VoiceHandoffRequestedV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "voice.handoff.requested"
    VERSION: ClassVar[int] = 1

    session_id: UUID
    call_id: UUID
    target: str
    correlation_id: str | None = None


@EVENT_REGISTRY.payload_model("voice.handoff.completed")
class VoiceHandoffCompletedV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "voice.handoff.completed"
    VERSION: ClassVar[int] = 1

    session_id: UUID
    call_id: UUID
    target: str
    correlation_id: str | None = None


@EVENT_REGISTRY.payload_model("voice.usage.recorded")
class VoiceUsageRecordedV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "voice.usage.recorded"
    VERSION: ClassVar[int] = 1

    session_id: UUID
    account_id: UUID
    audio_seconds_in: float
    audio_seconds_out: float
    provider_characters: int
    interruptions: int
    time_to_first_audio_ms: int | None = None
    session_duration_ms: int | None = None
    correlation_id: str | None = None


#: session state -> P04 event type. Only the states that warrant an outbox event.
SESSION_STATE_EVENT_TYPE: dict[str, str] = {
    "PENDING": "voice.session.created",
    "CONNECTING": "voice.session.connecting",
    "CONNECTED": "voice.session.connected",
    "STREAMING": "voice.session.streaming",
    "ENDING": "voice.session.ending",
    "COMPLETED": "voice.session.completed",
    "FAILED": "voice.session.failed",
    "CANCELLED": "voice.session.cancelled",
}
