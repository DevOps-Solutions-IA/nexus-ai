"""OTP audit event payloads (NXS-P10 with NXS-EVENT-010).

Registered in the canonical NXS-P04 payload registry. Payloads carry the challenge id,
the Organization (from the envelope), the purpose, the channel, a masked destination and
a stable result class — NEVER the OTP code, the code hash, the pepper or the full
destination. High-volume failed-attempt noise is bounded: a single
``otp.challenge.failed_attempt`` event per wrong attempt, and a distinct
``otp.challenge.locked`` when the limit is reached.
"""

from __future__ import annotations

from typing import ClassVar
from uuid import UUID

from nexus_ai.events.registry import EVENT_REGISTRY, EventPayload


class _OtpEventV1(EventPayload):
    challenge_id: UUID
    purpose: str
    channel: str
    masked_destination: str


@EVENT_REGISTRY.payload_model("otp.challenge.issued")
class OtpChallengeIssuedV1(_OtpEventV1):
    EVENT_TYPE: ClassVar[str] = "otp.challenge.issued"
    VERSION: ClassVar[int] = 1

    expires_at: str
    replayed: bool = False


@EVENT_REGISTRY.payload_model("otp.challenge.delivery_failed")
class OtpChallengeDeliveryFailedV1(_OtpEventV1):
    EVENT_TYPE: ClassVar[str] = "otp.challenge.delivery_failed"
    VERSION: ClassVar[int] = 1

    error_code: str


@EVENT_REGISTRY.payload_model("otp.challenge.verified")
class OtpChallengeVerifiedV1(_OtpEventV1):
    EVENT_TYPE: ClassVar[str] = "otp.challenge.verified"
    VERSION: ClassVar[int] = 1

    attempts: int


@EVENT_REGISTRY.payload_model("otp.challenge.failed_attempt")
class OtpChallengeFailedAttemptV1(_OtpEventV1):
    EVENT_TYPE: ClassVar[str] = "otp.challenge.failed_attempt"
    VERSION: ClassVar[int] = 1

    attempts: int
    max_attempts: int


@EVENT_REGISTRY.payload_model("otp.challenge.locked")
class OtpChallengeLockedV1(_OtpEventV1):
    EVENT_TYPE: ClassVar[str] = "otp.challenge.locked"
    VERSION: ClassVar[int] = 1

    attempts: int


@EVENT_REGISTRY.payload_model("otp.challenge.expired")
class OtpChallengeExpiredV1(_OtpEventV1):
    EVENT_TYPE: ClassVar[str] = "otp.challenge.expired"
    VERSION: ClassVar[int] = 1


@EVENT_REGISTRY.payload_model("otp.challenge.revoked")
class OtpChallengeRevokedV1(_OtpEventV1):
    EVENT_TYPE: ClassVar[str] = "otp.challenge.revoked"
    VERSION: ClassVar[int] = 1

    reason: str


@EVENT_REGISTRY.payload_model("otp.challenge.rate_limited")
class OtpChallengeRateLimitedV1(_OtpEventV1):
    EVENT_TYPE: ClassVar[str] = "otp.challenge.rate_limited"
    VERSION: ClassVar[int] = 1

    reason: str


#: reason -> event type, for the revoke path.
OTP_EVENT_ISSUED = "otp.challenge.issued"
OTP_EVENT_DELIVERY_FAILED = "otp.challenge.delivery_failed"
OTP_EVENT_VERIFIED = "otp.challenge.verified"
OTP_EVENT_FAILED_ATTEMPT = "otp.challenge.failed_attempt"
OTP_EVENT_LOCKED = "otp.challenge.locked"
OTP_EVENT_EXPIRED = "otp.challenge.expired"
OTP_EVENT_REVOKED = "otp.challenge.revoked"
OTP_EVENT_RATE_LIMITED = "otp.challenge.rate_limited"
