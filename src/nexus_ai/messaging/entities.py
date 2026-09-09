"""Messaging domain values and API contracts (NXS-P09).

Nothing here carries plaintext secret material: a :class:`MessagingAccount` references
a provider credential only through an opaque ``credential_ref`` resolved by the NXS-P07
vault seam. Provider-specific payloads are NEVER the canonical contract — a channel
adapter normalizes them into the shared objects below. Every request model is strict
(``extra="forbid"``) and bounded.
"""

from __future__ import annotations

import datetime as dt
import re
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

# --- bounded string aliases -------------------------------------------------------

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{1,62}$")]
ProviderKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,47}$")]
ExternalAccountId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
]
CredentialRef = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_:-]{2,126}$")]
IdempotencyKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{8,200}$")]
ProviderMessageId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
CorrelationId = Annotated[str, StringConstraints(max_length=128)]
Subject = Annotated[str, StringConstraints(max_length=255)]

#: Hard content bounds — enforced on every inbound and outbound path.
MAX_TEXT_BYTES = 64 * 1024
MAX_HTML_BYTES = 512 * 1024
MAX_SMS_TEXT_CHARS = 3200
MAX_RECIPIENTS = 50
MAX_WEBHOOK_BODY_BYTES = 1 * 1024 * 1024


class MessageChannel(StrEnum):
    WHATSAPP = "WHATSAPP"
    EMAIL = "EMAIL"
    SMS = "SMS"


class MessageDirection(StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class MessageStatus(StrEnum):
    #: Canonical, monotonic delivery lifecycle. Not every channel emits every state;
    #: a channel adapter maps provider status -> one of these (see delivery.py).
    RECEIVED = "RECEIVED"
    QUEUED = "QUEUED"
    SENDING = "SENDING"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    READ = "READ"
    FAILED = "FAILED"


class MessageContentType(StrEnum):
    TEXT = "TEXT"
    HTML = "HTML"
    MEDIA = "MEDIA"


class AccountStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class AddressKind(StrEnum):
    PHONE = "PHONE"
    EMAIL = "EMAIL"


#: Statuses from which no further transition is legal.
TERMINAL_STATUSES = frozenset({MessageStatus.READ, MessageStatus.FAILED})


# --- normalized shared domain objects --------------------------------------------


class MessageAddress(BaseModel):
    """A normalized participant address. Phone numbers are canonical E.164; email
    addresses are trimmed + lowercased. ``display`` is bounded, control-char-free."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: AddressKind
    value: Annotated[str, StringConstraints(min_length=1, max_length=320)]
    display: Annotated[str, StringConstraints(max_length=200)] | None = None


class MediaMetadata(BaseModel):
    """A SAFE bounded media boundary. Provider-supplied MIME / filename / size are
    UNTRUSTED and are stored only as declared hints — the Tool Engine never fetches a
    provider URL through unsafe code and never trusts these for a security decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_media_id: Annotated[str, StringConstraints(max_length=200)] | None = None
    declared_mime_type: Annotated[str, StringConstraints(max_length=128)] | None = None
    declared_filename: Annotated[str, StringConstraints(max_length=200)] | None = None
    declared_byte_size: Annotated[int, Field(ge=0, le=1_000_000_000)] | None = None
    caption: Annotated[str, StringConstraints(max_length=2000)] | None = None


class MessageContent(BaseModel):
    """The canonical, safe representation of a message body. HTML is stored as text and
    is NEVER executed or rendered by the platform."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content_type: MessageContentType = MessageContentType.TEXT
    text: Annotated[str, StringConstraints(max_length=MAX_TEXT_BYTES)] | None = None
    html: Annotated[str, StringConstraints(max_length=MAX_HTML_BYTES)] | None = None
    media: tuple[MediaMetadata, ...] = ()

    @model_validator(mode="after")
    def _coherent(self) -> MessageContent:
        if self.content_type is MessageContentType.TEXT and not self.text:
            raise ValueError("a TEXT message requires non-empty text")
        if self.content_type is MessageContentType.HTML and not (self.html or self.text):
            raise ValueError("an HTML message requires html or text")
        if self.content_type is MessageContentType.MEDIA and not self.media:
            raise ValueError("a MEDIA message requires at least one media item")
        return self


class EmailEnvelopeFields(BaseModel):
    """Email-only normalized headers. Threading keys are bounded, control-char-free."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: Subject | None = None
    cc: tuple[MessageAddress, ...] = ()
    bcc: tuple[MessageAddress, ...] = ()
    message_id_header: Annotated[str, StringConstraints(max_length=255)] | None = None
    in_reply_to: Annotated[str, StringConstraints(max_length=255)] | None = None
    references: tuple[Annotated[str, StringConstraints(max_length=255)], ...] = ()


class SmsSegmentInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    encoding: Annotated[str, StringConstraints(max_length=32)] | None = None
    segment_count: Annotated[int, Field(ge=1, le=20)] | None = None


class Message(BaseModel):
    """A persisted, normalized channel message. Contains no provider secret."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    conversation_id: UUID
    customer_id: UUID | None
    channel: MessageChannel
    direction: MessageDirection
    status: MessageStatus
    provider: str
    provider_account_id: UUID
    provider_message_id: str | None
    sender: MessageAddress
    recipients: tuple[MessageAddress, ...]
    content: MessageContent
    email: EmailEnvelopeFields | None
    sms: SmsSegmentInfo | None
    reply_to_message_id: UUID | None
    correlation_id: str | None
    idempotency_key: str | None
    error_code: str | None
    provider_timestamp: dt.datetime | None
    sent_at: dt.datetime | None
    delivered_at: dt.datetime | None
    read_at: dt.datetime | None
    failed_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime

    def public_view(self) -> MessageView:
        return MessageView(**self.model_dump())


class MessageView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    conversation_id: UUID
    customer_id: UUID | None
    channel: MessageChannel
    direction: MessageDirection
    status: MessageStatus
    provider: str
    provider_account_id: UUID
    provider_message_id: str | None
    sender: MessageAddress
    recipients: tuple[MessageAddress, ...]
    content: MessageContent
    email: EmailEnvelopeFields | None
    sms: SmsSegmentInfo | None
    reply_to_message_id: UUID | None
    correlation_id: str | None
    idempotency_key: str | None
    error_code: str | None
    provider_timestamp: dt.datetime | None
    sent_at: dt.datetime | None
    delivered_at: dt.datetime | None
    read_at: dt.datetime | None
    failed_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime


# --- messaging account model ------------------------------------------------------


class MessagingAccount(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    channel: MessageChannel
    provider: str
    slug: str
    external_account_id: str
    sender_identity: str
    credential_ref: str | None
    status: AccountStatus
    configuration: dict[str, Any]
    webhook_token: str
    created_at: dt.datetime
    updated_at: dt.datetime

    def public_view(self) -> MessagingAccountView:
        return MessagingAccountView(
            id=self.id,
            organization_id=self.organization_id,
            channel=self.channel,
            provider=self.provider,
            slug=self.slug,
            external_account_id=self.external_account_id,
            sender_identity=self.sender_identity,
            has_credential=self.credential_ref is not None,
            status=self.status,
            configuration=self.configuration,
            receive_path=f"/api/v1/webhooks/messaging/{self.provider}/{self.webhook_token}",
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class MessagingAccountView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    channel: MessageChannel
    provider: str
    slug: str
    external_account_id: str
    sender_identity: str
    has_credential: bool
    status: AccountStatus
    configuration: dict[str, Any]
    receive_path: str
    created_at: dt.datetime
    updated_at: dt.datetime


# --- API request models ----------------------------------------------------------


class CreateAccountRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: MessageChannel
    provider: ProviderKey
    slug: Slug
    external_account_id: ExternalAccountId
    #: The tenant-owned sender identity: an E.164 phone (WhatsApp / SMS) or an email
    #: address (Email). Validated + normalized against the channel on create.
    sender_identity: Annotated[str, StringConstraints(min_length=1, max_length=320)]
    default_country: Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,2}$")] | None = None
    configuration: dict[str, Any] = Field(default_factory=dict)


class UpdateAccountRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    configuration: dict[str, Any] | None = None


_CREDENTIAL_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


class StoreAccountCredentialRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Opaque provider-secret fields (e.g. {"api_token": "...", "webhook_secret": "..."}).
    #: Bounded; stored only in the vault; never returned.
    fields: dict[str, Annotated[str, StringConstraints(min_length=1, max_length=4096)]]

    @model_validator(mode="after")
    def _bounded(self) -> StoreAccountCredentialRequest:
        if not self.fields or len(self.fields) > 8:
            raise ValueError("between 1 and 8 credential fields are required")
        for name in self.fields:
            if not _CREDENTIAL_FIELD_NAME.match(name):
                raise ValueError(f"credential field name {name!r} is not a valid identifier")
        return self


class OutboundAddressInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    value: Annotated[str, StringConstraints(min_length=1, max_length=320)]
    display: Annotated[str, StringConstraints(max_length=200)] | None = None


class SendMessageRequest(BaseModel):
    """The one governed way to send. No provider id, URL, method, headers, raw SMTP
    command or arbitrary provider header — the caller names an account and a
    conversation and supplies validated content only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: UUID
    conversation_id: UUID
    to: tuple[OutboundAddressInput, ...]
    content: MessageContent
    subject: Subject | None = None
    reply_to_message_id: UUID | None = None
    idempotency_key: IdempotencyKey | None = None
    correlation_id: CorrelationId | None = None

    @model_validator(mode="after")
    def _bounded_recipients(self) -> SendMessageRequest:
        if not self.to:
            raise ValueError("at least one recipient is required")
        if len(self.to) > MAX_RECIPIENTS:
            raise ValueError(f"at most {MAX_RECIPIENTS} recipients are allowed")
        return self
