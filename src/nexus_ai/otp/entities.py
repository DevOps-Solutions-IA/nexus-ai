"""OTP domain values and API contracts (NXS-P10).

Nothing here carries a plaintext OTP after issuance: :class:`OtpChallenge` holds a keyed
hash only, and every view / result model returns a *masked* destination and safe
lifecycle metadata. Every request model is strict (``extra="forbid"``) and bounded.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# --- bounded string aliases -----------------------------------------------------

#: A registered OTP purpose. An allow-listed, stable token — never a free-form string.
PurposeKey = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{2,47}$")]
Destination = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=320)]
IdempotencyKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{8,200}$")]
CorrelationId = Annotated[str, StringConstraints(max_length=128)]
#: A submitted code: digits only, bounded to the configured maximum length.
SubmittedCode = Annotated[str, StringConstraints(pattern=r"^[0-9]{1,12}$")]

#: The single keyed-verifier version currently emitted. Bumping this is a deliberate
#: migration event (old challenges verify against their stored ``hash_version``).
CURRENT_HASH_VERSION = 1


class OtpChannel(StrEnum):
    """Delivery channels an OTP may use. Intentionally a strict subset of the NXS-P09
    message channels — an OTP is never delivered over a channel whose semantics do not
    suit a short-lived secret."""

    SMS = "SMS"
    EMAIL = "EMAIL"


class OtpSubjectType(StrEnum):
    #: A raw normalized destination (phone / email) with no Customer identity attached.
    DESTINATION = "DESTINATION"


class OtpStatus(StrEnum):
    #: Issued and verifiable.
    ACTIVE = "ACTIVE"
    #: A correct code was accepted — terminal, single-use.
    VERIFIED = "VERIFIED"
    #: TTL elapsed before verification — terminal.
    EXPIRED = "EXPIRED"
    #: Superseded by a newer issuance or explicitly revoked — terminal.
    REVOKED = "REVOKED"
    #: Attempt limit reached — terminal, a correct code no longer verifies.
    LOCKED = "LOCKED"


#: Statuses from which no verification can ever succeed.
TERMINAL_STATUSES = frozenset(
    {OtpStatus.VERIFIED, OtpStatus.EXPIRED, OtpStatus.REVOKED, OtpStatus.LOCKED}
)


class OtpVerifyOutcome(StrEnum):
    VERIFIED = "VERIFIED"


class OtpChallenge(BaseModel):
    """The durable challenge record. ``code_hash`` is an HMAC keyed by the configured
    pepper over the canonical challenge context and the code — the plaintext code is
    never stored, logged or returned."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    subject_type: OtpSubjectType
    destination: str
    destination_fingerprint: str
    purpose: str
    channel: OtpChannel
    messaging_account_id: UUID
    code_hash: str
    hash_version: int
    status: OtpStatus
    attempts: int
    max_attempts: int
    request_fingerprint: str
    idempotency_key: str | None
    delivery_message_id: UUID | None
    correlation_id: str | None
    issued_at: dt.datetime
    expires_at: dt.datetime
    resend_after: dt.datetime
    verified_at: dt.datetime | None
    revoked_at: dt.datetime | None
    locked_at: dt.datetime | None
    last_attempt_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime

    def public_view(self, *, masked_destination: str) -> OtpChallengeView:
        return OtpChallengeView(
            challenge_id=self.id,
            organization_id=self.organization_id,
            purpose=self.purpose,
            channel=self.channel,
            status=self.status,
            masked_destination=masked_destination,
            attempts=self.attempts,
            max_attempts=self.max_attempts,
            issued_at=self.issued_at,
            expires_at=self.expires_at,
            resend_after=self.resend_after,
        )


# --- API contracts -------------------------------------------------------------


class IssueOtpRequest(BaseModel):
    """The one governed way to issue an OTP. The caller names a purpose, a delivery
    channel, a bounded destination and a configured NXS-P09 messaging account — never a
    provider, URL, header, template body or the code itself."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    purpose: PurposeKey
    channel: OtpChannel
    destination: Destination
    messaging_account_id: UUID
    default_country: Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,2}$")] | None = None
    idempotency_key: IdempotencyKey | None = None
    correlation_id: CorrelationId | None = None


class IssueOtpResult(BaseModel):
    """The safe issuance result. It never contains the code — only the metadata a caller
    needs to prompt for and later verify it."""

    model_config = ConfigDict(frozen=True)

    challenge_id: UUID
    status: OtpStatus
    purpose: str
    channel: OtpChannel
    masked_destination: str
    expires_at: dt.datetime
    resend_after: dt.datetime
    max_attempts: int
    delivery: OtpDeliveryStatus
    replayed: bool = False


class OtpDeliveryStatus(StrEnum):
    #: The messaging service accepted the send.
    SENT = "SENT"
    #: Replayed issuance — no new send was performed.
    SKIPPED = "SKIPPED"
    #: The provider call timed out — delivery is AMBIGUOUS. The challenge stays active
    #: so a code that did arrive can still be entered; it is never blindly resent.
    UNCONFIRMED = "UNCONFIRMED"


class VerifyOtpRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: SubmittedCode
    correlation_id: CorrelationId | None = None


class VerifyOtpResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    challenge_id: UUID
    outcome: OtpVerifyOutcome
    verified_at: dt.datetime


class OtpChallengeView(BaseModel):
    model_config = ConfigDict(frozen=True)

    challenge_id: UUID
    organization_id: UUID
    purpose: str
    channel: OtpChannel
    status: OtpStatus
    masked_destination: str
    attempts: int
    max_attempts: int
    issued_at: dt.datetime
    expires_at: dt.datetime
    resend_after: dt.datetime


class OtpChallengeCreate(BaseModel):
    """Internal: the fully-resolved row the service persists (post generation + hashing).
    Not an API surface."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    subject_type: OtpSubjectType
    destination: str
    destination_fingerprint: str
    purpose: str
    channel: OtpChannel
    messaging_account_id: UUID
    code_hash: str
    hash_version: int
    max_attempts: int = Field(ge=1, le=10)
    request_fingerprint: str
    idempotency_key: str | None
    correlation_id: str | None
    issued_at: dt.datetime
    expires_at: dt.datetime
    resend_after: dt.datetime
