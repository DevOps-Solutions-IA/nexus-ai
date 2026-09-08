"""Email channel adapter (NXS-EMAIL-001).

Provider abstraction for a JSON-over-HTTPS transactional email provider (the common
shape of Postmark / SendGrid / Mailgun-style APIs). Inbound is a signed HTTP webhook —
there is NO raw SMTP command surface. HTML is transported as opaque text and is never
executed. Header fields are CRLF-safe (see :mod:`nexus_ai.messaging.content`). A caller
never supplies arbitrary provider headers.

Account shape:
* ``external_account_id`` — the provider mailbox / stream id;
* ``sender_identity``     — the From address;
* ``configuration``       — optional ``{"api_base": "https://api.example.com"}``;
* credential fields       — ``api_token`` (Bearer), ``webhook_secret`` (HMAC-SHA256).
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from nexus_ai.integrations.credentials import SecretMaterial
from nexus_ai.messaging.content import scrub_header_token
from nexus_ai.messaging.entities import (
    EmailEnvelopeFields,
    MessageChannel,
    MessageContent,
    MessageContentType,
    MessageStatus,
    MessagingAccount,
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
    "sent": MessageStatus.SENT,
    "delivered": MessageStatus.DELIVERED,
    "opened": MessageStatus.READ,
    "read": MessageStatus.READ,
    "bounced": MessageStatus.FAILED,
    "failed": MessageStatus.FAILED,
    "dropped": MessageStatus.FAILED,
}


def _parse_ts(raw: Any) -> dt.datetime | None:
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _thread_root(message_id: str, in_reply_to: str | None, references: list[str]) -> str:
    for candidate in (*references, in_reply_to or "", message_id):
        cleaned = candidate.strip().strip("<>")
        if cleaned:
            return cleaned[:128]
    return message_id[:128]


class EmailProvider:
    channel = MessageChannel.EMAIL
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
        kind = str(payload.get("type") or ("message" if payload.get("from") else "")).lower()
        if kind in ("message", "inbound", "inbound_email"):
            inbound = self._normalize_inbound(payload, account)
            if inbound is not None:
                result.event_ids.append(f"msg:{inbound.provider_message_id}")
                result.inbound.append(inbound)
            return result
        status = self._normalize_status(payload)
        if status is not None:
            result.event_ids.append(f"st:{status.provider_message_id}:{status.provider_status}")
            result.statuses.append(status)
        return result

    def _normalize_inbound(
        self, payload: dict[str, Any], account: MessagingAccount
    ) -> NormalizedInbound | None:
        sender = str(payload.get("from") or "").strip()
        provider_message_id = scrub_header_token(
            str(payload.get("message_id") or payload.get("id") or "").strip().strip("<>")
        )
        if not sender or not provider_message_id:
            return None
        recipients = payload.get("to") or []
        recipient = str(
            recipients[0]
            if isinstance(recipients, list) and recipients
            else account.sender_identity
        ).strip()
        references = [
            token
            for ref in (payload.get("references") or [])
            if (token := scrub_header_token(str(ref).strip().strip("<>")))
        ][:20]
        in_reply_to = (
            scrub_header_token(str(payload.get("in_reply_to") or "").strip().strip("<>")) or None
        )
        text = str(payload.get("text") or "")[:60000]
        html_raw = payload.get("html")
        html = str(html_raw)[:400000] if html_raw else None
        content = MessageContent(
            content_type=MessageContentType.HTML if html else MessageContentType.TEXT,
            text=text or "(empty)",
            html=html,
        )
        email = EmailEnvelopeFields(
            subject=str(payload.get("subject") or "")[:255] or None,
            message_id_header=provider_message_id[:255],
            in_reply_to=in_reply_to,
            references=tuple(references),
        )
        return NormalizedInbound(
            provider_message_id=provider_message_id,
            provider_timestamp=_parse_ts(payload.get("timestamp") or payload.get("date")),
            sender_raw=sender,
            recipient_raw=recipient,
            content=content,
            external_thread_hint=_thread_root(provider_message_id, in_reply_to, references),
            email=email,
        )

    @staticmethod
    def _normalize_status(payload: dict[str, Any]) -> NormalizedStatus | None:
        provider_message_id = (
            str(payload.get("message_id") or payload.get("id") or "").strip().strip("<>")
        )
        provider_status = str(payload.get("status") or payload.get("event") or "").strip().lower()
        canonical = _STATUS_MAP.get(provider_status)
        if not provider_message_id or canonical is None:
            return None
        return NormalizedStatus(
            provider_message_id=provider_message_id,
            status=canonical,
            provider_status=provider_status,
            occurred_at=_parse_ts(payload.get("timestamp")),
            error_code=str(payload.get("reason") or "")[:120] or None,
        )

    async def send(
        self,
        account: MessagingAccount,
        prepared: PreparedSend,
        secret: SecretMaterial | None,
        transport: MessagingTransport,
    ) -> ProviderSendResult:
        if secret is None:
            raise MessagingProviderError("the email account has no api token configured")
        email = prepared.email or EmailEnvelopeFields()
        headers: dict[str, str] = {}
        if email.message_id_header:
            headers["Message-ID"] = f"<{email.message_id_header}>"
        if email.in_reply_to:
            headers["In-Reply-To"] = f"<{email.in_reply_to}>"
        if email.references:
            headers["References"] = " ".join(f"<{ref}>" for ref in email.references)
        document = {
            "from": prepared.sender.value,
            "to": [address.value for address in prepared.recipients],
            "cc": [address.value for address in email.cc],
            "bcc": [address.value for address in email.bcc],
            "subject": prepared.subject or "",
            "text": prepared.content.text or "",
            "html": prepared.content.html,
            "headers": headers,
        }
        base = str(account.configuration.get("api_base") or _DEFAULT_API_BASE).rstrip("/")
        try:
            response = await transport.request(
                method="POST",
                url=f"{base}/email",
                headers={
                    "Authorization": f"Bearer {secret.field('api_token')}",
                    "Content-Type": "application/json",
                },
                body=json.dumps(document).encode("utf-8"),
                timeout_seconds=20.0,
            )
        except TransportError as exc:
            if exc.timeout:
                raise MessagingTimeoutError("the email provider timed out") from exc
            raise MessagingProviderError(
                "the email provider connection failed", retryable=not exc.connect
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
                "the email provider rejected the message",
                status_code=status_code,
                provider_code=str(code)[:64] if code is not None else None,
            )
        message_id = str(payload.get("message_id") or payload.get("id") or "").strip().strip("<>")
        if not message_id:
            raise MessagingProviderError("the email provider returned no message id")
        return ProviderSendResult(provider_message_id=message_id, status=MessageStatus.SENT)
