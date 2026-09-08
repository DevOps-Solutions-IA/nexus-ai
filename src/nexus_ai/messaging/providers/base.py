"""The messaging provider abstraction (NXS-P09, ADR-0070).

A provider is a normalizer, not a transport. It never opens a socket itself — every
outbound HTTP call goes through the injected :class:`MessagingTransport` (production
wires the NXS-P07 governed executor: SSRF-safe, bounded timeouts, TLS verified, no raw
header injection). Channels differ, so the protocol does not force an identical
transport API — it forces an identical *normalized domain output*.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from nexus_ai.integrations.credentials import SecretMaterial
from nexus_ai.messaging.entities import (
    EmailEnvelopeFields,
    MessageAddress,
    MessageChannel,
    MessageContent,
    MessageStatus,
    MessagingAccount,
    SmsSegmentInfo,
)


@dataclass(frozen=True, slots=True)
class WebhookContext:
    method: str
    headers: Mapping[str, str]
    query: Mapping[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


class MessagingTransport(Protocol):
    async def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float | None = None,
    ) -> TransportResponse: ...


class TransportError(Exception):
    """A transport failure with an honest ambiguity flag."""

    def __init__(self, message: str, *, timeout: bool = False, connect: bool = False) -> None:
        super().__init__(message)
        self.timeout = timeout
        self.connect = connect


def provider_send_error(detail: str, *, status_code: int, provider_code: str | None) -> Exception:
    """Map a provider HTTP status to the shared messaging error taxonomy. 429 ->
    NXS_MSG_RATE_LIMITED, 504 -> NXS_MSG_TIMEOUT (ambiguous, never blindly retried),
    everything else -> NXS_MSG_PROVIDER_ERROR (retryable only on 5xx)."""
    from nexus_ai.messaging.errors import (
        MessagingProviderError,
        MessagingRateLimitedError,
        MessagingTimeoutError,
    )

    if status_code == 429:
        return MessagingRateLimitedError(detail)
    if status_code == 504:
        return MessagingTimeoutError(detail)
    return MessagingProviderError(
        detail,
        provider_code=provider_code,
        provider_status=status_code,
        retryable=status_code in (500, 502, 503),
    )


@dataclass(frozen=True, slots=True)
class NormalizedInbound:
    provider_message_id: str
    provider_timestamp: dt.datetime | None
    sender_raw: str
    recipient_raw: str
    content: MessageContent
    #: A deterministic provider thread hint (e.g. an email References root, a WhatsApp
    #: phone pair). The inbound pipeline turns this into the P06 external_thread_id.
    external_thread_hint: str
    email: EmailEnvelopeFields | None = None


@dataclass(frozen=True, slots=True)
class NormalizedStatus:
    provider_message_id: str
    status: MessageStatus
    provider_status: str
    occurred_at: dt.datetime | None
    error_code: str | None = None
    provider_code: str | None = None


@dataclass(frozen=True, slots=True)
class WebhookParseResult:
    #: A stable per-event id used for inbound dedupe / replay protection.
    event_ids: list[str] = field(default_factory=list)
    inbound: list[NormalizedInbound] = field(default_factory=list)
    statuses: list[NormalizedStatus] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PreparedSend:
    sender: MessageAddress
    recipients: tuple[MessageAddress, ...]
    content: MessageContent
    subject: str | None
    email: EmailEnvelopeFields | None
    reply_provider_message_id: str | None


@dataclass(frozen=True, slots=True)
class ProviderSendResult:
    provider_message_id: str
    status: MessageStatus
    provider_timestamp: dt.datetime | None = None
    sms: SmsSegmentInfo | None = None


@runtime_checkable
class MessagingProvider(Protocol):
    channel: MessageChannel
    provider_key: str

    def webhook_challenge(self, account: MessagingAccount, ctx: WebhookContext) -> str | None:
        """Return the challenge string a provider GET verification expects, or None."""
        ...

    async def verify_webhook(
        self, account: MessagingAccount, ctx: WebhookContext, secret: SecretMaterial | None
    ) -> None:
        """Raise a messaging error if the webhook is not authentic."""
        ...

    def parse_webhook(self, account: MessagingAccount, ctx: WebhookContext) -> WebhookParseResult:
        """Normalize an authenticated webhook body. Never trusts an organization id in
        the payload."""
        ...

    async def send(
        self,
        account: MessagingAccount,
        prepared: PreparedSend,
        secret: SecretMaterial | None,
        transport: MessagingTransport,
    ) -> ProviderSendResult:
        """Deliver one outbound message through the governed transport."""
        ...
