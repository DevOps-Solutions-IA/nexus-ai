"""SMS channel adapter (NXS-SMS-001).

Provider-neutral inbound / outbound SMS over a JSON-over-HTTPS provider. This module is
the generic delivery mechanism that NXS-P10 later consumes for OTP — it contains NO OTP
generation, entropy, hashing, expiry, attempt-limit or verification logic.

Account shape:
* ``external_account_id`` — the provider messaging-service / sender id;
* ``sender_identity``     — the sending number in E.164;
* ``configuration``       — optional ``{"api_base": "https://api.example.com"}``;
* credential fields       — ``api_token`` (Bearer), ``webhook_secret`` (HMAC-SHA256).
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from nexus_ai.integrations.credentials import SecretMaterial
from nexus_ai.messaging.entities import (
    MessageChannel,
    MessageContent,
    MessageContentType,
    MessageStatus,
    MessagingAccount,
    SmsSegmentInfo,
)
from nexus_ai.messaging.errors import (
    MessagingPayloadInvalidError,
    MessagingProviderError,
    MessagingSignatureInvalidError,
    MessagingTimeoutError,
)
from nexus_ai.messaging.providers.base import (
    MessagingTransport,
    NormalizedInbound,
    NormalizedStatus,
    PreparedSend,
    ProviderSendResult,
    TransportError,
    WebhookContext,
    WebhookParseResult,
    provider_send_error,
    verify_generic_signed_webhook,
)
from nexus_ai.messaging.providers.whatsapp import _header

_DEFAULT_API_BASE = "https://api.messaging.local"
_STATUS_MAP: dict[str, MessageStatus] = {
    "queued": MessageStatus.QUEUED,
    "accepted": MessageStatus.QUEUED,
    "sending": MessageStatus.SENDING,
    "sent": MessageStatus.SENT,
    "delivered": MessageStatus.DELIVERED,
    "undelivered": MessageStatus.FAILED,
    "failed": MessageStatus.FAILED,
}


def _parse_ts(raw: Any) -> dt.datetime | None:
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _digits(raw: str) -> str:
    stripped = raw.strip()
    return f"+{stripped.lstrip('+')}" if stripped else stripped


class SmsProvider:
    channel = MessageChannel.SMS
    provider_key = "generic_http"

    def webhook_challenge(self, account: MessagingAccount, ctx: WebhookContext) -> str | None:
        return None

    async def verify_webhook(
        self,
        account: MessagingAccount,
        ctx: WebhookContext,
        secret: SecretMaterial | None,
        *,
        timestamp_tolerance_seconds: int,
    ) -> None:
        if secret is None:
            raise MessagingSignatureInvalidError("the account has no webhook secret configured")
        verify_generic_signed_webhook(
            body=ctx.body,
            provided_signature=_header(ctx.headers, "x-messaging-signature"),
            provided_timestamp=_header(ctx.headers, "x-messaging-timestamp"),
            secret=secret.field("webhook_secret"),
            tolerance_seconds=timestamp_tolerance_seconds,
        )

    def parse_webhook(self, account: MessagingAccount, ctx: WebhookContext) -> WebhookParseResult:
        try:
            payload = json.loads(ctx.body or b"{}")
        except ValueError as exc:
            raise MessagingPayloadInvalidError("the webhook body is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise MessagingPayloadInvalidError("the webhook body must be a JSON object")

        result = WebhookParseResult()
        if payload.get("status"):
            status = self._normalize_status(payload)
            if status is not None:
                result.event_ids.append(f"st:{status.provider_message_id}:{status.provider_status}")
                result.statuses.append(status)
            return result
        inbound = self._normalize_inbound(payload, account)
        if inbound is not None:
            result.event_ids.append(f"msg:{inbound.provider_message_id}")
            result.inbound.append(inbound)
        return result

    def _normalize_inbound(
        self, payload: dict[str, Any], account: MessagingAccount
    ) -> NormalizedInbound | None:
        sender = _digits(str(payload.get("from") or ""))
        provider_message_id = str(payload.get("message_id") or payload.get("sid") or "").strip()
        if not sender or not provider_message_id:
            return None
        recipient = _digits(str(payload.get("to") or account.sender_identity))
        text = str(payload.get("text") or payload.get("body") or "")[:3200]
        pair = "-".join(sorted((sender, recipient)))
        return NormalizedInbound(
            provider_message_id=provider_message_id,
            provider_timestamp=_parse_ts(payload.get("timestamp")),
            sender_raw=sender,
            recipient_raw=recipient,
            content=MessageContent(content_type=MessageContentType.TEXT, text=text or "(empty)"),
            external_thread_hint=pair,
        )

    @staticmethod
    def _normalize_status(payload: dict[str, Any]) -> NormalizedStatus | None:
        provider_message_id = str(payload.get("message_id") or payload.get("sid") or "").strip()
        provider_status = str(payload.get("status") or "").strip().lower()
        canonical = _STATUS_MAP.get(provider_status)
        if not provider_message_id or canonical is None:
            return None
        return NormalizedStatus(
            provider_message_id=provider_message_id,
            status=canonical,
            provider_status=provider_status,
            occurred_at=_parse_ts(payload.get("timestamp")),
            error_code=str(payload.get("error_code") or "")[:120] or None,
        )

    async def send(
        self,
        account: MessagingAccount,
        prepared: PreparedSend,
        secret: SecretMaterial | None,
        transport: MessagingTransport,
    ) -> ProviderSendResult:
        if secret is None:
            raise MessagingProviderError("the SMS account has no api token configured")
        if len(prepared.recipients) != 1:
            raise MessagingProviderError("SMS delivers to exactly one recipient per send")
        document = {
            "from": prepared.sender.value,
            "to": prepared.recipients[0].value,
            "text": prepared.content.text or "",
            "messaging_service_id": account.external_account_id,
        }
        base = str(account.configuration.get("api_base") or _DEFAULT_API_BASE).rstrip("/")
        try:
            response = await transport.request(
                method="POST",
                url=f"{base}/sms",
                headers={
                    "Authorization": f"Bearer {secret.field('api_token')}",
                    "Content-Type": "application/json",
                },
                body=json.dumps(document).encode("utf-8"),
                timeout_seconds=15.0,
            )
        except TransportError as exc:
            if exc.timeout:
                raise MessagingTimeoutError("the SMS provider timed out") from exc
            raise MessagingProviderError(
                "the SMS provider connection failed", retryable=not exc.connect
            ) from exc
        return self._send_result(response.status_code, response.body)

    @staticmethod
    def _send_result(status_code: int, raw: bytes) -> ProviderSendResult:
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}
        if status_code >= 400 or not isinstance(payload, dict):
            code = payload.get("error_code") if isinstance(payload, dict) else None
            raise provider_send_error(
                "the SMS provider rejected the message",
                status_code=status_code,
                provider_code=str(code)[:64] if code is not None else None,
            )
        message_id = str(payload.get("message_id") or payload.get("sid") or "").strip()
        if not message_id:
            raise MessagingProviderError("the SMS provider returned no message id")
        segments = payload.get("segments")
        sms_info = (
            SmsSegmentInfo(
                encoding=str(payload.get("encoding") or "")[:32] or None,
                segment_count=int(segments) if isinstance(segments, int) and segments > 0 else None,
            )
            if (payload.get("encoding") or segments)
            else None
        )
        return ProviderSendResult(
            provider_message_id=message_id, status=MessageStatus.SENT, sms=sms_info
        )
