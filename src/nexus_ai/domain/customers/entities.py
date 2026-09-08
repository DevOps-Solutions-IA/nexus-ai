"""Customer and conversation domain values (NXS-CUSTOMER-001).

ORM rows never cross the service boundary. Views carry identifiers, never raw
normalized identity values beyond what the caller legitimately owns (identity
listings are the caller's own tenant data). No secrets exist anywhere in the model.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints

_Trimmed = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
_Channel = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
_ActivityType = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){0,3}$")
]


class IdentityType(StrEnum):
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    EXTERNAL_ID = "EXTERNAL_ID"


class IdentityVerificationState(StrEnum):
    UNVERIFIED = "UNVERIFIED"
    VERIFIED = "VERIFIED"
    REVOKED = "REVOKED"


class CustomerStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class IdentityStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class ConversationStatus(StrEnum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class ParticipantType(StrEnum):
    CUSTOMER = "CUSTOMER"
    HUMAN_AGENT = "HUMAN_AGENT"
    AI_AGENT = "AI_AGENT"
    SYSTEM = "SYSTEM"


class ActivityType(StrEnum):
    CUSTOMER_CREATED = "customer.created"
    IDENTITY_LINKED = "identity.linked"
    IDENTITY_STATUS_CHANGED = "identity.status_changed"
    CONVERSATION_OPENED = "conversation.opened"
    CONVERSATION_CLOSED = "conversation.closed"
    MESSAGE_INBOUND = "message.inbound"
    MESSAGE_OUTBOUND = "message.outbound"


class Customer(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    display_name: str
    status: CustomerStatus
    preferred_locale: str | None
    version: int
    created_at: dt.datetime
    updated_at: dt.datetime

    def public_view(self) -> CustomerView:
        return CustomerView(
            id=self.id,
            organization_id=self.organization_id,
            display_name=self.display_name,
            status=self.status,
            preferred_locale=self.preferred_locale,
            version=self.version,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class CustomerView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    display_name: str
    status: CustomerStatus
    preferred_locale: str | None
    version: int
    created_at: dt.datetime
    updated_at: dt.datetime


class CustomerIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    customer_id: UUID
    identity_type: IdentityType
    normalized_value: str
    verification_state: IdentityVerificationState
    is_primary: bool
    source: str
    status: IdentityStatus
    created_at: dt.datetime
    updated_at: dt.datetime

    def public_view(self) -> CustomerIdentityView:
        return CustomerIdentityView(
            id=self.id,
            customer_id=self.customer_id,
            identity_type=self.identity_type,
            normalized_value=self.normalized_value,
            verification_state=self.verification_state,
            is_primary=self.is_primary,
            source=self.source,
            status=self.status,
            created_at=self.created_at,
        )


class CustomerIdentityView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    customer_id: UUID
    identity_type: IdentityType
    normalized_value: str
    verification_state: IdentityVerificationState
    is_primary: bool
    source: str
    status: IdentityStatus
    created_at: dt.datetime


class Conversation(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    customer_id: UUID | None
    channel: str
    status: ConversationStatus
    provider_namespace: str | None
    external_thread_id: str | None
    subject: str | None
    version: int
    opened_at: dt.datetime
    last_activity_at: dt.datetime
    closed_at: dt.datetime | None

    def public_view(self) -> ConversationView:
        return ConversationView(
            id=self.id,
            organization_id=self.organization_id,
            customer_id=self.customer_id,
            channel=self.channel,
            status=self.status,
            provider_namespace=self.provider_namespace,
            external_thread_id=self.external_thread_id,
            subject=self.subject,
            version=self.version,
            opened_at=self.opened_at,
            last_activity_at=self.last_activity_at,
            closed_at=self.closed_at,
        )


class ConversationView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    customer_id: UUID | None
    channel: str
    status: ConversationStatus
    provider_namespace: str | None
    external_thread_id: str | None
    subject: str | None
    version: int
    opened_at: dt.datetime
    last_activity_at: dt.datetime
    closed_at: dt.datetime | None


class ConversationParticipant(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    conversation_id: UUID
    participant_type: ParticipantType
    participant_ref: str
    joined_at: dt.datetime
    left_at: dt.datetime | None


class TimelineActivityView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    customer_id: UUID | None
    conversation_id: UUID | None
    activity_type: str
    occurred_at: dt.datetime
    data: dict[str, object]


# --- request models ---------------------------------------------------------------


class CreateCustomerRequest(BaseModel):
    """Create (or resolve) a Customer. Exactly one initial identity is required —
    resolve-or-create is the documented duplicate-safe pattern (ADR-0052)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    display_name: _Trimmed
    preferred_locale: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=32)] | None
    ) = None
    identity_type: IdentityType
    identity_value: Annotated[str, StringConstraints(min_length=1, max_length=320)]
    identity_source: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")] = (
        "manual"
    )
    default_country: Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,2}$")] | None = None


class LinkIdentityRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    identity_type: IdentityType
    identity_value: Annotated[str, StringConstraints(min_length=1, max_length=320)]
    identity_source: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")] = (
        "manual"
    )
    default_country: Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,2}$")] | None = None


class CreateConversationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    customer_id: UUID | None = None
    channel: _Channel
    provider_namespace: (
        Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{0,47}$")] | None
    ) = None
    external_thread_id: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
        | None
    ) = None
    subject: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
