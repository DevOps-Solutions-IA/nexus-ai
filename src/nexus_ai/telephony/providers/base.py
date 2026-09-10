"""The telephony provider adapter contract (NXS-P11, ADR-0084).

A ``TelephonyProviderAdapter`` is a *normalizer*: it turns provider-specific REST calls
and webhook payloads into the shared telephony domain objects, and nothing else. It
never opens a raw socket, never runs a shell, never emits a dialplan and never returns a
provider credential. Every outbound provider HTTP call goes through an injected
``TelephonyTransport`` (the NXS-P07 governed HTTP executor in production).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from nexus_ai.telephony.entities import (
    CallParticipant,
    NormalizedCallEvent,
    NormalizedInboundCall,
    TelephonyAccount,
)
from nexus_ai.telephony.errors import (
    TelephonyProviderError,
    TelephonyProviderTimeoutError,
    TelephonyWebhookInvalidError,
    TelephonyWebhookReplayError,
)


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes


class TransportError(Exception):
    def __init__(self, message: str, *, timeout: bool = False, connect: bool = False) -> None:
        super().__init__(message)
        self.timeout = timeout
        self.connect = connect


class TelephonyTransport(Protocol):
    async def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float | None = None,
    ) -> TransportResponse: ...


@dataclass(frozen=True, slots=True)
class WebhookContext:
    method: str
    headers: dict[str, str]
    query: dict[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class OutboundCallSpec:
    """Everything an adapter needs to place a call — already validated / canonicalized by
    the service. No raw header, no dialplan, no credential."""

    account: TelephonyAccount
    caller_id_e164: str
    caller_display_name: str | None
    destination_kind: str  # "PHONE" | "SIP_ALIAS"
    destination_value: str
    correlation_id: str | None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OutboundCallResult:
    provider_call_id: str
    accepted: bool


@dataclass(frozen=True, slots=True)
class WebhookParseResult:
    inbound_call: NormalizedInboundCall | None = None
    events: tuple[NormalizedCallEvent, ...] = ()
    challenge_response: str | None = None


class TelephonyProviderAdapter(Protocol):
    """The stable provider contract future phases consume."""

    key: str

    async def create_outbound_call(
        self, spec: OutboundCallSpec, secret: Any, transport: TelephonyTransport
    ) -> OutboundCallResult: ...

    async def hangup_call(
        self,
        account: TelephonyAccount,
        provider_call_id: str,
        secret: Any,
        transport: TelephonyTransport,
    ) -> None: ...

    async def send_dtmf(
        self,
        account: TelephonyAccount,
        provider_call_id: str,
        digits: str,
        secret: Any,
        transport: TelephonyTransport,
    ) -> None: ...

    def verify_webhook(
        self, account: TelephonyAccount, ctx: WebhookContext, secret: Any, *, tolerance_seconds: int
    ) -> None: ...

    def parse_webhook(
        self, account: TelephonyAccount, ctx: WebhookContext
    ) -> WebhookParseResult: ...

    def webhook_challenge(self, account: TelephonyAccount, ctx: WebhookContext) -> str | None: ...


# --- shared helpers -------------------------------------------------------------


def verify_signed_webhook(
    *,
    body: bytes,
    provided_signature: str | None,
    provided_timestamp: str | None,
    secret: str,
    tolerance_seconds: int,
) -> None:
    """The generic telephony signed-webhook protocol: HMAC-SHA256 over
    ``"<unix_ts>." + body`` with a MANDATORY, freshness-checked timestamp.

    * missing signature / timestamp -> NXS_TELEPHONY_WEBHOOK_INVALID (explicit);
    * malformed timestamp            -> NXS_TELEPHONY_WEBHOOK_INVALID;
    * tampered body / timestamp      -> NXS_TELEPHONY_WEBHOOK_INVALID (constant-time HMAC);
    * correctly-signed but stale     -> NXS_TELEPHONY_WEBHOOK_REPLAY.
    """
    if not provided_signature:
        raise TelephonyWebhookInvalidError("the provider callback is unsigned")
    if not provided_timestamp:
        raise TelephonyWebhookInvalidError("the signed callback requires a timestamp header")
    try:
        timestamp_value = int(provided_timestamp.strip())
    except (TypeError, ValueError) as exc:
        raise TelephonyWebhookInvalidError("the callback timestamp is malformed") from exc

    signed_payload = f"{provided_timestamp}.".encode() + body
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    signature = provided_signature.strip()
    digest = signature.split("=", 1)[1] if "=" in signature else signature
    if not hmac.compare_digest(expected, digest.lower()):
        raise TelephonyWebhookInvalidError("the callback signature did not verify")
    if abs(time.time() - timestamp_value) > tolerance_seconds:
        raise TelephonyWebhookReplayError(
            "the callback timestamp is outside the permitted freshness window"
        )


def provider_call_failure(
    detail: str, *, status_code: int, provider_code: str | None = None
) -> TelephonyProviderError | TelephonyProviderTimeoutError:
    if status_code == 504:
        return TelephonyProviderTimeoutError("the telephony provider timed out")
    return TelephonyProviderError(
        detail,
        provider_code=provider_code,
        provider_status=status_code,
        retryable=status_code in (429, 500, 502, 503),
    )


def pstn_participant(e164: str, display_name: str | None = None) -> CallParticipant:
    from nexus_ai.telephony.entities import ParticipantKind

    return CallParticipant(kind=ParticipantKind.PSTN, address=e164, display_name=display_name)
