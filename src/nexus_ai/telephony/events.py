"""Telephony audit event payloads (NXS-P11 with NXS-EVENT-011).

Registered in the canonical NXS-P04 payload registry. Payloads carry IDs and safe
lifecycle metadata ONLY — never a SIP credential, an ARI/AMI credential, an Authorization
header, a provider token, a raw SDP body or a full provider payload. The trusted
``organization_id`` comes from the envelope.
"""

from __future__ import annotations

from typing import ClassVar
from uuid import UUID

from nexus_ai.events.registry import EVENT_REGISTRY, EventPayload


class _CallEventV1(EventPayload):
    call_id: UUID
    account_id: UUID
    direction: str
    provider: str
    state: str
    provider_call_id: str | None = None
    correlation_id: str | None = None


@EVENT_REGISTRY.payload_model("telephony.call.created")
class CallCreatedV1(_CallEventV1):
    EVENT_TYPE: ClassVar[str] = "telephony.call.created"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("telephony.call.ringing")
class CallRingingV1(_CallEventV1):
    EVENT_TYPE: ClassVar[str] = "telephony.call.ringing"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("telephony.call.answered")
class CallAnsweredV1(_CallEventV1):
    EVENT_TYPE: ClassVar[str] = "telephony.call.answered"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("telephony.call.bridged")
class CallBridgedV1(_CallEventV1):
    EVENT_TYPE: ClassVar[str] = "telephony.call.bridged"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("telephony.call.ending")
class CallEndingV1(_CallEventV1):
    EVENT_TYPE: ClassVar[str] = "telephony.call.ending"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("telephony.call.completed")
class CallCompletedV1(_CallEventV1):
    EVENT_TYPE: ClassVar[str] = "telephony.call.completed"
    VERSION: ClassVar[int] = 1

    disposition: str


@EVENT_REGISTRY.payload_model("telephony.call.failed")
class CallFailedV1(_CallEventV1):
    EVENT_TYPE: ClassVar[str] = "telephony.call.failed"
    VERSION: ClassVar[int] = 1

    disposition: str
    error_code: str | None = None


@EVENT_REGISTRY.payload_model("telephony.call.busy")
class CallBusyV1(_CallEventV1):
    EVENT_TYPE: ClassVar[str] = "telephony.call.busy"
    VERSION: ClassVar[int] = 1

    disposition: str


@EVENT_REGISTRY.payload_model("telephony.call.no_answer")
class CallNoAnswerV1(_CallEventV1):
    EVENT_TYPE: ClassVar[str] = "telephony.call.no_answer"
    VERSION: ClassVar[int] = 1

    disposition: str


@EVENT_REGISTRY.payload_model("telephony.call.cancelled")
class CallCancelledV1(_CallEventV1):
    EVENT_TYPE: ClassVar[str] = "telephony.call.cancelled"
    VERSION: ClassVar[int] = 1

    disposition: str


@EVENT_REGISTRY.payload_model("telephony.dtmf.received")
class DtmfReceivedV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "telephony.dtmf.received"
    VERSION: ClassVar[int] = 1

    call_id: UUID
    account_id: UUID
    digit: str
    correlation_id: str | None = None


@EVENT_REGISTRY.payload_model("telephony.media.started")
class MediaStartedV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "telephony.media.started"
    VERSION: ClassVar[int] = 1

    call_id: UUID
    account_id: UUID
    media_session_id: UUID
    direction: str
    correlation_id: str | None = None


@EVENT_REGISTRY.payload_model("telephony.media.stopped")
class MediaStoppedV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "telephony.media.stopped"
    VERSION: ClassVar[int] = 1

    call_id: UUID
    account_id: UUID
    media_session_id: UUID
    direction: str
    correlation_id: str | None = None


#: call state -> P04 event type. Only the states that warrant an outbox event.
STATE_EVENT_TYPE: dict[str, str] = {
    "CREATED": "telephony.call.created",
    "RINGING": "telephony.call.ringing",
    "ANSWERED": "telephony.call.answered",
    "BRIDGED": "telephony.call.bridged",
    "ENDING": "telephony.call.ending",
    "COMPLETED": "telephony.call.completed",
    "FAILED": "telephony.call.failed",
    "BUSY": "telephony.call.busy",
    "NO_ANSWER": "telephony.call.no_answer",
    "CANCELLED": "telephony.call.cancelled",
}
