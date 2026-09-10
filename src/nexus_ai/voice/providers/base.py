"""The voice provider adapter contract (NXS-P12, ADR-0087).

A ``VoiceProviderAdapter`` is a *normalizer*: it turns provider-specific REST calls,
WebSocket messages and webhook payloads into the shared voice domain objects, and
nothing else. It never owns Organization authorization, NXS-P11 call ownership, business
logic, tool execution or agent reasoning. Every provider REST call goes through an
injected :class:`VoiceHttpTransport` (the NXS-P07 governed HTTP executor in production);
every WebSocket goes through a :class:`~nexus_ai.voice.transport.VoiceStreamTransport`.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from nexus_ai.voice.audio import AudioFormat
from nexus_ai.voice.entities import VoiceProviderEvent
from nexus_ai.voice.errors import (
    VoiceProviderError,
    VoiceProviderTimeoutError,
    VoiceWebhookInvalidError,
    VoiceWebhookReplayError,
)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes


class HttpError(Exception):
    def __init__(self, message: str, *, timeout: bool = False, connect: bool = False) -> None:
        super().__init__(message)
        self.timeout = timeout
        self.connect = connect


class VoiceHttpTransport(Protocol):
    async def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float | None = None,
    ) -> HttpResponse: ...


@dataclass(frozen=True, slots=True)
class VoiceSessionSpec:
    """Everything an adapter needs to open a session — already validated by the service.
    No credential, no raw provider config, no WebSocket URL.

    ``provider_api_base`` is the TRUSTED provider REST origin. It comes from server-side
    configuration (``settings.voice.elevenlabs_api_base``), NEVER from a caller: a
    ``start_session`` request cannot select the provider endpoint.
    """

    provider: str
    external_account_id: str
    provider_voice_ref: str
    provider_model_ref: str | None
    input_format: AudioFormat
    output_format: AudioFormat
    #: Trusted provider REST origin (a bare https origin), from server config only.
    provider_api_base: str = ""
    #: Bounded, provider-neutral, ALLOW-LISTED session options — never an endpoint.
    options: dict[str, str] = field(default_factory=dict)
    correlation_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderSessionInit:
    """The result of the REST leg: how to open the real-time transport, plus the audio
    format the provider actually negotiated. ``url`` is a short-lived signed WebSocket URL
    that has ALREADY been validated against the provider host allow-list
    (:func:`validate_provider_ws_url`); ``ws_host`` / ``ws_port`` are the validated TCP
    target the transport must pin so a cross-origin redirect cannot escape. The URL is
    never logged, evented or persisted."""

    url: str
    headers: dict[str, str]
    negotiated_format: AudioFormat
    ws_host: str
    ws_port: int
    provider_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class WebhookContext:
    method: str
    headers: dict[str, str]
    query: dict[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class WebhookParseResult:
    provider_event_id: str | None = None
    session_ended: bool = False
    usage_characters: int | None = None
    duration_ms: int | None = None


class VoiceProviderAdapter(Protocol):
    """The stable provider contract future phases consume."""

    key: str

    async def open_session(
        self, spec: VoiceSessionSpec, secret: Any, http: VoiceHttpTransport
    ) -> ProviderSessionInit: ...

    def serialize_init(self, spec: VoiceSessionSpec) -> bytes | str: ...

    def serialize_audio(self, pcm: bytes) -> bytes | str: ...

    def keepalive_reply(self, sequence: int | None) -> bytes | str | None: ...

    def parse_frame(self, raw: bytes | str) -> VoiceProviderEvent | None: ...

    def verify_webhook(
        self, ctx: WebhookContext, secret: Any, *, tolerance_seconds: int
    ) -> None: ...

    def parse_webhook(self, ctx: WebhookContext) -> WebhookParseResult: ...


# --- shared helpers -----------------------------------------------------------------


_UNSAFE_WS_PORTS: frozenset[int] = frozenset({22, 23, 25, 3306, 5432, 6379, 9200, 11211, 27017})


@dataclass(frozen=True, slots=True)
class ValidatedWsTarget:
    url: str
    host: str
    port: int


def validate_provider_ws_url(url: str, *, allowed_hosts: frozenset[str]) -> ValidatedWsTarget:
    """The one gate a provider-supplied signed WebSocket URL passes before the transport
    opens it. Defends against a chain of: caller-influenced REST -> attacker-controlled
    signed_url -> a connection to an internal / arbitrary host.

    Fails closed (``NXS_VOICE_PROVIDER_ERROR``) when the URL is not ``wss://``, carries
    userinfo, targets an IP literal, targets a loopback / private / link-local /
    multicast / reserved / cloud-metadata address, uses an unsafe port, or its host is
    not in ``allowed_hosts``. The returned host / port are what the transport MUST pin so
    a cross-origin redirect cannot escape.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        raise VoiceProviderError("the provider returned a malformed signed url") from exc
    if parts.scheme != "wss":
        raise VoiceProviderError("the provider signed url is not a wss:// url")
    if parts.username or parts.password:
        raise VoiceProviderError("the provider signed url carries userinfo")
    host = (parts.hostname or "").lower()
    if not host:
        raise VoiceProviderError("the provider signed url has no host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        # An IP literal is never a legitimate provider endpoint and is the SSRF vector.
        raise VoiceProviderError("the provider signed url targets an IP literal")
    if host not in allowed_hosts:
        raise VoiceProviderError("the provider signed url host is not on the allow-list")
    try:
        port = parts.port if parts.port is not None else 443
    except ValueError as exc:
        raise VoiceProviderError("the provider signed url has an invalid port") from exc
    if port in _UNSAFE_WS_PORTS or not (1 <= port <= 65_535):
        raise VoiceProviderError("the provider signed url uses an unsafe port")
    return ValidatedWsTarget(url=url, host=host, port=port)


def verify_elevenlabs_style_signature(
    *,
    body: bytes,
    header_value: str | None,
    secret: str,
    tolerance_seconds: int,
) -> None:
    """Verify an ``ElevenLabs-Signature: t=<unix>,v0=<hex hmac>`` header — HMAC-SHA256 over
    ``"<t>." + body`` with a MANDATORY, freshness-checked timestamp.

    * missing / malformed header  -> NXS_VOICE_WEBHOOK_INVALID;
    * tampered body / signature   -> NXS_VOICE_WEBHOOK_INVALID (constant-time compare);
    * correctly-signed but stale  -> NXS_VOICE_WEBHOOK_REPLAY.
    """
    if not header_value:
        raise VoiceWebhookInvalidError("the provider callback is unsigned")
    parts = {
        piece.split("=", 1)[0].strip(): piece.split("=", 1)[1].strip()
        for piece in header_value.split(",")
        if "=" in piece
    }
    timestamp_raw = parts.get("t")
    signature = parts.get("v0")
    if not timestamp_raw or not signature:
        raise VoiceWebhookInvalidError("the callback signature header is malformed")
    try:
        timestamp_value = int(timestamp_raw)
    except ValueError as exc:
        raise VoiceWebhookInvalidError("the callback timestamp is malformed") from exc

    signed_payload = f"{timestamp_raw}.".encode() + body
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.lower()):
        raise VoiceWebhookInvalidError("the callback signature did not verify")
    if abs(time.time() - timestamp_value) > tolerance_seconds:
        raise VoiceWebhookReplayError(
            "the callback timestamp is outside the permitted freshness window"
        )


def provider_rest_error(
    detail: str, *, status_code: int, provider_code: str | None = None
) -> VoiceProviderError | VoiceProviderTimeoutError:
    if status_code == 504:
        return VoiceProviderTimeoutError("the voice provider timed out")
    return VoiceProviderError(
        detail,
        provider_code=provider_code,
        provider_status=status_code,
        retryable=status_code in (429, 500, 502, 503),
    )
