"""A fake telephony provider (NXS-P11).

Deterministic, no socket, no credential. Used by tests and available as a first-class
provider so an Organization can exercise the full call-control surface without a carrier.
Its signed-webhook protocol is the generic ``verify_signed_webhook`` (HMAC over
``"<ts>." + body`` with an ``X-Telephony-Timestamp`` header).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from nexus_ai.telephony.entities import (
    CallDisposition,
    CallParticipant,
    CallState,
    DtmfDigit,
    MediaDirection,
    MediaState,
    NormalizedCallEvent,
    NormalizedInboundCall,
    ParticipantKind,
    TelephonyAccount,
    TelephonyEventType,
)
from nexus_ai.telephony.errors import TelephonyProviderError, TelephonyWebhookInvalidError
from nexus_ai.telephony.providers.base import (
    OutboundCallResult,
    OutboundCallSpec,
    TelephonyTransport,
    TransportError,
    WebhookContext,
    WebhookParseResult,
    provider_call_failure,
    verify_signed_webhook,
)

_STATE_BY_NAME = {state.value: state for state in CallState}
_DISPOSITION_BY_NAME = {d.value: d for d in CallDisposition}
_DIGIT_BY_VALUE = {d.value: d for d in DtmfDigit}


class FakeTelephonyProvider:
    key = "fake"

    async def create_outbound_call(
        self, spec: OutboundCallSpec, secret: Any, transport: TelephonyTransport
    ) -> OutboundCallResult:
        payload = json.dumps(
            {
                "from": spec.caller_id_e164,
                "to": spec.destination_value,
                "kind": spec.destination_kind,
            }
        ).encode()
        try:
            response = await transport.request(
                method="POST",
                url="https://fake.telephony.local/calls",
                headers={"Content-Type": "application/json"},
                body=payload,
                timeout_seconds=None,
            )
        except TransportError as exc:
            if exc.timeout:
                from nexus_ai.telephony.errors import TelephonyProviderTimeoutError

                raise TelephonyProviderTimeoutError("the fake provider timed out") from exc
            raise TelephonyProviderError(
                "the fake provider connection failed", retryable=not exc.connect
            ) from exc
        if response.status_code >= 400:
            raise provider_call_failure(
                "the fake provider rejected the call", status_code=response.status_code
            )
        body = json.loads(response.body or b"{}")
        provider_call_id = str(body.get("call_id") or f"fake-{uuid.uuid4().hex}")
        return OutboundCallResult(provider_call_id=provider_call_id, accepted=True)

    async def hangup_call(
        self,
        account: TelephonyAccount,
        provider_call_id: str,
        secret: Any,
        transport: TelephonyTransport,
    ) -> None:
        try:
            response = await transport.request(
                method="DELETE",
                url=f"https://fake.telephony.local/calls/{provider_call_id}",
                headers={},
                body=None,
                timeout_seconds=None,
            )
        except TransportError as exc:
            if exc.timeout:
                from nexus_ai.telephony.errors import TelephonyProviderTimeoutError

                raise TelephonyProviderTimeoutError("the fake provider timed out") from exc
            raise TelephonyProviderError("the fake provider connection failed") from exc
        if response.status_code >= 400 and response.status_code != 404:
            raise provider_call_failure(
                "the fake provider rejected the hangup", status_code=response.status_code
            )

    async def send_dtmf(
        self,
        account: TelephonyAccount,
        provider_call_id: str,
        digits: str,
        secret: Any,
        transport: TelephonyTransport,
    ) -> None:
        response = await transport.request(
            method="POST",
            url=f"https://fake.telephony.local/calls/{provider_call_id}/dtmf",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"digits": digits}).encode(),
            timeout_seconds=None,
        )
        if response.status_code >= 400:
            raise provider_call_failure(
                "the fake provider rejected the DTMF", status_code=response.status_code
            )

    def verify_webhook(
        self,
        account: TelephonyAccount,
        ctx: WebhookContext,
        secret: Any,
        *,
        tolerance_seconds: int,
    ) -> None:
        secret_value = _webhook_secret(secret)
        headers = {k.lower(): v for k, v in ctx.headers.items()}
        verify_signed_webhook(
            body=ctx.body,
            provided_signature=headers.get("x-telephony-signature"),
            provided_timestamp=headers.get("x-telephony-timestamp"),
            secret=secret_value,
            tolerance_seconds=tolerance_seconds,
        )

    def webhook_challenge(self, account: TelephonyAccount, ctx: WebhookContext) -> str | None:
        return ctx.query.get("challenge")

    def parse_webhook(self, account: TelephonyAccount, ctx: WebhookContext) -> WebhookParseResult:
        try:
            payload = json.loads(ctx.body or b"{}")
        except json.JSONDecodeError as exc:
            raise TelephonyProviderError("the callback body is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise TelephonyProviderError("the callback body must be a JSON object")

        kind = str(payload.get("kind") or "").lower()
        if kind == "inbound":
            return WebhookParseResult(inbound_call=_parse_inbound(payload))
        return WebhookParseResult(events=(_parse_event(payload),))


def _webhook_secret(secret: Any) -> str:
    from nexus_ai.integrations.errors import IntegrationCredentialUnavailableError

    if secret is None:
        raise TelephonyWebhookInvalidError("the account has no webhook secret configured")
    try:
        return str(secret.field("webhook_secret"))
    except IntegrationCredentialUnavailableError as exc:
        raise TelephonyWebhookInvalidError("the account has no webhook secret configured") from exc


def _parse_inbound(payload: dict[str, Any]) -> NormalizedInboundCall:
    return NormalizedInboundCall(
        provider_event_id=str(payload["event_id"]),
        provider_call_id=str(payload["call_id"]),
        dialed_number=str(payload["to"]),
        caller=CallParticipant(
            kind=ParticipantKind.PSTN,
            address=str(payload["from"]),
            display_name=_bounded(payload.get("caller_name")),
        ),
        provider_timestamp=_parse_ts(payload.get("timestamp")),
        correlation_id=_bounded(payload.get("correlation_id")),
    )


def _parse_event(payload: dict[str, Any]) -> NormalizedCallEvent:
    event_kind = str(payload.get("event") or "").upper()
    provider_event_id = str(payload["event_id"])
    provider_call_id = str(payload["call_id"])
    ts = _parse_ts(payload.get("timestamp"))
    seq = payload.get("sequence")
    seq_value = int(seq) if isinstance(seq, int) else None

    if event_kind == "DTMF":
        digit = _DIGIT_BY_VALUE.get(str(payload.get("digit") or ""))
        if digit is None:
            raise TelephonyProviderError("the callback carried an unknown DTMF digit")
        return NormalizedCallEvent(
            provider_event_id=provider_event_id,
            provider_call_id=provider_call_id,
            event_type=TelephonyEventType.DTMF,
            digit=digit,
            provider_timestamp=ts,
            provider_sequence=seq_value,
        )
    if event_kind == "MEDIA":
        media_state = {s.value: s for s in MediaState}.get(str(payload.get("media_state") or ""))
        if media_state is None:
            raise TelephonyProviderError("the callback carried an unknown media state")
        media_dir = {d.value: d for d in MediaDirection}.get(
            str(payload.get("media_direction") or "BIDIRECTIONAL")
        )
        return NormalizedCallEvent(
            provider_event_id=provider_event_id,
            provider_call_id=provider_call_id,
            event_type=TelephonyEventType.MEDIA,
            media_state=media_state,
            media_direction=media_dir,
            bridge_id=_bounded(payload.get("bridge_id")),
            stream_id=_bounded(payload.get("stream_id")),
            provider_timestamp=ts,
            provider_sequence=seq_value,
        )

    state = _STATE_BY_NAME.get(event_kind)
    if state is None:
        raise TelephonyProviderError(f"the callback carried an unknown state {event_kind!r}")
    disposition = _DISPOSITION_BY_NAME.get(str(payload.get("disposition") or ""))
    return NormalizedCallEvent(
        provider_event_id=provider_event_id,
        provider_call_id=provider_call_id,
        event_type=TelephonyEventType.STATE,
        state=state,
        disposition=disposition,
        provider_timestamp=ts,
        provider_sequence=seq_value,
    )


def _bounded(value: Any, limit: int = 200) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or any(ord(ch) < 0x20 for ch in text):
        return None
    return text[:limit]


def _parse_ts(value: Any) -> Any:
    import datetime as dt

    if value is None:
        return None
    try:
        if isinstance(value, int | float):
            return dt.datetime.fromtimestamp(float(value), tz=dt.UTC)
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except TypeError, ValueError:
        return None
