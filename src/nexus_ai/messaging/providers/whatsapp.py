"""WhatsApp channel adapter (NXS-WA-001).

Provider-neutral boundary with a Meta WhatsApp Cloud API reference implementation.
Nothing here trusts a provider-supplied organization id, MIME type, filename or URL. The
outbound HTTP call goes through the injected governed transport only.

Account shape:
* ``external_account_id`` — the WhatsApp phone-number id;
* ``sender_identity``     — the business number in E.164;
* ``configuration``       — optional ``{"graph_base": "...", "api_version": "v19.0"}``;
* credential fields       — ``access_token`` (Bearer), ``app_secret`` (X-Hub-Signature-256),
                            ``verify_token`` (GET challenge).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

from nexus_ai.integrations.credentials import SecretMaterial
from nexus_ai.messaging.entities import (
    MediaMetadata,
    MessageChannel,
    MessageContent,
    MessageContentType,
    MessageStatus,
    MessagingAccount,
)
from nexus_ai.messaging.errors import (
    MessagingChallengeFailedError,
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
)

_DEFAULT_GRAPH_BASE = "https://graph.facebook.com"
_DEFAULT_API_VERSION = "v19.0"
_MAX_BATCH = 100

_STATUS_MAP: dict[str, MessageStatus] = {
    "sent": MessageStatus.SENT,
    "delivered": MessageStatus.DELIVERED,
    "read": MessageStatus.READ,
    "failed": MessageStatus.FAILED,
}


def _timestamp(raw: Any) -> dt.datetime | None:
    with contextlib.suppress(TypeError, ValueError):
        return dt.datetime.fromtimestamp(int(raw), tz=dt.UTC)
    return None


class WhatsAppProvider:
    channel = MessageChannel.WHATSAPP
    provider_key = "meta_cloud"

    def webhook_challenge(self, account: MessagingAccount, ctx: WebhookContext) -> str | None:
        if ctx.method.upper() != "GET":
            return None
        if ctx.query.get("hub.mode") != "subscribe":
            raise MessagingChallengeFailedError("unexpected hub.mode")
        return ctx.query.get("hub.challenge") or ""

    async def verify_webhook(
        self,
        account: MessagingAccount,
        ctx: WebhookContext,
        secret: SecretMaterial | None,
        *,
        timestamp_tolerance_seconds: int,
    ) -> None:
        # Meta's X-Hub-Signature-256 carries no timestamp protocol; the tolerance
        # argument is accepted for a uniform provider interface and unused here.
        _ = timestamp_tolerance_seconds
        if ctx.method.upper() == "GET":
            token = ctx.query.get("hub.verify_token")
            expected = secret.field("verify_token") if secret is not None else None
            if not expected or not token or not hmac.compare_digest(token, expected):
                raise MessagingChallengeFailedError("the webhook verify token did not match")
            return
        if secret is None:
            raise MessagingSignatureInvalidError("the account has no signing secret configured")
        provided = _header(ctx.headers, "x-hub-signature-256")
        if not provided:
            raise MessagingSignatureInvalidError("the request is unsigned")
        digest = provided.split("=", 1)[1] if "=" in provided else provided
        expected = hmac.new(
            secret.field("app_secret").encode("utf-8"), ctx.body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, digest.lower()):
            raise MessagingSignatureInvalidError("the webhook signature did not verify")

    def parse_webhook(self, account: MessagingAccount, ctx: WebhookContext) -> WebhookParseResult:
        try:
            payload = json.loads(ctx.body or b"{}")
        except ValueError as exc:
            raise MessagingPayloadInvalidError("the webhook body is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise MessagingPayloadInvalidError("the webhook body must be a JSON object")

        result = WebhookParseResult()
        entries = payload.get("entry") or []
        if not isinstance(entries, list) or len(entries) > _MAX_BATCH:
            raise MessagingPayloadInvalidError("the webhook batch is malformed or too large")
        for entry in entries:
            for change in (entry or {}).get("changes", []) or []:
                value = (change or {}).get("value") or {}
                business_number = str(
                    (value.get("metadata") or {}).get("display_phone_number")
                    or account.sender_identity
                )
                for raw in (value.get("messages") or [])[:_MAX_BATCH]:
                    inbound = self._normalize_inbound(raw, business_number)
                    if inbound is not None:
                        result.event_ids.append(f"msg:{inbound.provider_message_id}")
                        result.inbound.append(inbound)
                for raw in (value.get("statuses") or [])[:_MAX_BATCH]:
                    status = self._normalize_status(raw)
                    if status is not None:
                        result.event_ids.append(
                            f"st:{status.provider_message_id}:{status.provider_status}"
                        )
                        result.statuses.append(status)
        return result

    def _normalize_inbound(self, raw: Any, business_number: str) -> NormalizedInbound | None:
        if not isinstance(raw, dict):
            return None
        provider_message_id = str(raw.get("id") or "").strip()
        sender = str(raw.get("from") or "").strip()
        if not provider_message_id or not sender:
            return None
        message_type = raw.get("type")
        content = self._inbound_content(raw, message_type)
        pair = "-".join(sorted((f"+{sender.lstrip('+')}", f"+{business_number.lstrip('+')}")))
        return NormalizedInbound(
            provider_message_id=provider_message_id,
            provider_timestamp=_timestamp(raw.get("timestamp")),
            sender_raw=f"+{sender.lstrip('+')}",
            recipient_raw=f"+{business_number.lstrip('+')}",
            content=content,
            external_thread_hint=pair,
        )

    @staticmethod
    def _inbound_content(raw: dict[str, Any], message_type: Any) -> MessageContent:
        if message_type == "text":
            body = str(((raw.get("text") or {}).get("body")) or "")[:16000]
            return MessageContent(content_type=MessageContentType.TEXT, text=body or "(empty)")
        # image / audio / video / document / sticker: SAFE bounded metadata only, never a
        # trusted MIME or a fetched URL.
        media_block = raw.get(str(message_type)) or {}
        return MessageContent(
            content_type=MessageContentType.MEDIA,
            text=None,
            media=(
                MediaMetadata(
                    provider_media_id=str(media_block.get("id") or "")[:200] or None,
                    declared_mime_type=str(media_block.get("mime_type") or "")[:128] or None,
                    declared_filename=str(media_block.get("filename") or "")[:200] or None,
                    caption=str(media_block.get("caption") or "")[:2000] or None,
                ),
            ),
        )

    @staticmethod
    def _normalize_status(raw: Any) -> NormalizedStatus | None:
        if not isinstance(raw, dict):
            return None
        provider_message_id = str(raw.get("id") or "").strip()
        provider_status = str(raw.get("status") or "").strip().lower()
        canonical = _STATUS_MAP.get(provider_status)
        if not provider_message_id or canonical is None:
            return None
        errors = raw.get("errors") or []
        first_error = errors[0] if isinstance(errors, list) and errors else {}
        return NormalizedStatus(
            provider_message_id=provider_message_id,
            status=canonical,
            provider_status=provider_status,
            occurred_at=_timestamp(raw.get("timestamp")),
            error_code=str(first_error.get("title") or "")[:120] or None,
            provider_code=str(first_error.get("code") or "")[:64] or None,
        )

    async def send(
        self,
        account: MessagingAccount,
        prepared: PreparedSend,
        secret: SecretMaterial | None,
        transport: MessagingTransport,
    ) -> ProviderSendResult:
        if secret is None:
            raise MessagingProviderError("the WhatsApp account has no access token configured")
        if len(prepared.recipients) != 1:
            raise MessagingProviderError("WhatsApp delivers to exactly one recipient per send")
        recipient = prepared.recipients[0].value
        text = prepared.content.text or ""
        body = json.dumps(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient.lstrip("+"),
                "type": "text",
                "text": {"body": text},
            }
        ).encode("utf-8")
        base = str(account.configuration.get("graph_base") or _DEFAULT_GRAPH_BASE).rstrip("/")
        version = str(account.configuration.get("api_version") or _DEFAULT_API_VERSION)
        url = f"{base}/{version}/{account.external_account_id}/messages"
        try:
            response = await transport.request(
                method="POST",
                url=url,
                headers={
                    "Authorization": f"Bearer {secret.field('access_token')}",
                    "Content-Type": "application/json",
                },
                body=body,
                timeout_seconds=15.0,
            )
        except TransportError as exc:
            if exc.timeout:
                raise MessagingTimeoutError("the WhatsApp provider timed out") from exc
            raise MessagingProviderError(
                "the WhatsApp provider connection failed", retryable=not exc.connect
            ) from exc
        return self._send_result(response.status_code, response.body)

    @staticmethod
    def _send_result(status_code: int, raw: bytes) -> ProviderSendResult:
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}
        if status_code >= 400 or not isinstance(payload, dict):
            error = (payload.get("error") or {}) if isinstance(payload, dict) else {}
            raise provider_send_error(
                "the WhatsApp provider rejected the message",
                status_code=status_code,
                provider_code=str(error.get("code") or "")[:64] or None,
            )
        messages = payload.get("messages") or []
        wamid = str(messages[0].get("id")) if messages and isinstance(messages[0], dict) else ""
        if not wamid:
            raise MessagingProviderError("the WhatsApp provider returned no message id")
        return ProviderSendResult(provider_message_id=wamid, status=MessageStatus.SENT)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None
