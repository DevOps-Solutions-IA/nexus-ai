"""Messaging pure-logic primitives (NXS-P09)."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from nexus_ai.core.errors import NxsError
from nexus_ai.messaging.addresses import normalize_address
from nexus_ai.messaging.content import normalize_content, sanitize_header_value
from nexus_ai.messaging.delivery import apply_callback, can_transition, require_transition
from nexus_ai.messaging.entities import (
    AddressKind,
    MessageChannel,
    MessageContent,
    MessageContentType,
    MessageStatus,
    SendMessageRequest,
)
from nexus_ai.messaging.errors import (
    MESSAGING_ERRORS,
    MessagingPayloadInvalidError,
    MessagingRecipientInvalidError,
    MessagingStateConflictError,
)
from nexus_ai.messaging.idempotency import send_fingerprint
from nexus_ai.messaging.providers.base import WebhookContext
from nexus_ai.messaging.providers.email import EmailProvider
from nexus_ai.messaging.providers.sms import SmsProvider
from nexus_ai.messaging.providers.whatsapp import WhatsAppProvider

pytestmark = pytest.mark.anyio


# --- addresses ----------------------------------------------------------------


def test_phone_address_is_canonical_e164() -> None:
    address = normalize_address(MessageChannel.WHATSAPP, " +1 (415) 555-0142 ")
    assert address.kind is AddressKind.PHONE
    assert address.value == "+14155550142"


def test_email_address_is_lowercased_and_trimmed() -> None:
    address = normalize_address(MessageChannel.EMAIL, "  Ada.Lovelace@Example.COM ")
    assert address.kind is AddressKind.EMAIL
    assert address.value == "ada.lovelace@example.com"


def test_ambiguous_phone_without_country_fails_closed() -> None:
    with pytest.raises(MessagingRecipientInvalidError):
        normalize_address(MessageChannel.SMS, "4155550142")


def test_address_display_name_control_chars_stripped() -> None:
    address = normalize_address(MessageChannel.EMAIL, "ada@example.com", display="Ada\r\nEvil: x")
    assert address.display is None


# --- content -----------------------------------------------------------------


def test_sms_rejects_html_and_overlong_text() -> None:
    with pytest.raises(MessagingPayloadInvalidError):
        normalize_content(
            MessageChannel.SMS,
            MessageContent(content_type=MessageContentType.TEXT, text="x" * 4000),
        )


def test_html_is_rejected_on_non_email_channels() -> None:
    with pytest.raises(MessagingPayloadInvalidError):
        normalize_content(
            MessageChannel.WHATSAPP,
            MessageContent(content_type=MessageContentType.HTML, text="hi", html="<b>hi</b>"),
        )


def test_html_is_kept_as_opaque_text_for_email() -> None:
    content = normalize_content(
        MessageChannel.EMAIL,
        MessageContent(
            content_type=MessageContentType.HTML,
            text="hi",
            html="<script>alert(1)</script>",
        ),
    )
    assert content.html == "<script>alert(1)</script>"  # stored, never executed


def test_header_value_rejects_crlf_and_nul() -> None:
    for bad in ("subject\r\nBcc: attacker@evil.com", "a\nb", "a\x00b"):
        with pytest.raises(MessagingPayloadInvalidError):
            sanitize_header_value(bad, field="subject")


def test_body_rejects_control_characters_and_bad_unicode() -> None:
    with pytest.raises(MessagingPayloadInvalidError):
        normalize_content(
            MessageChannel.EMAIL,
            MessageContent(content_type=MessageContentType.TEXT, text="hi\x07there"),
        )


# --- delivery state machine -------------------------------------------------


def test_delivery_state_advances_monotonically() -> None:
    out = apply_callback(MessageStatus.SENT, MessageStatus.DELIVERED)
    assert out.advanced and out.status is MessageStatus.DELIVERED


def test_regressive_callback_is_ignored_not_applied() -> None:
    out = apply_callback(MessageStatus.READ, MessageStatus.SENT)
    assert not out.advanced and out.status is MessageStatus.READ


def test_duplicate_same_state_callback_is_idempotent() -> None:
    out = apply_callback(MessageStatus.DELIVERED, MessageStatus.DELIVERED)
    assert out.duplicate and not out.advanced


def test_failed_is_reachable_from_any_non_terminal_state_but_terminal_is_sticky() -> None:
    assert can_transition(MessageStatus.QUEUED, MessageStatus.FAILED)
    assert not can_transition(MessageStatus.FAILED, MessageStatus.SENT)
    with pytest.raises(MessagingStateConflictError):
        require_transition(MessageStatus.READ, MessageStatus.SENT)


# --- idempotency fingerprint ----------------------------------------------


def _send_request(recipients: list[str], text: str = "hi") -> SendMessageRequest:
    import uuid

    return SendMessageRequest(
        account_id=uuid.UUID(int=1),
        conversation_id=uuid.UUID(int=2),
        to=tuple({"value": value} for value in recipients),  # type: ignore[misc]
        content=MessageContent(content_type=MessageContentType.TEXT, text=text),
    )


def test_send_fingerprint_is_deterministic_and_recipient_order_independent() -> None:
    a = send_fingerprint(_send_request(["+14155550142"]), ["+14155550142"])
    b = send_fingerprint(_send_request(["+14155550142"]), ["+14155550142"])
    assert a == b
    ordered = send_fingerprint(_send_request(["+1", "+2"]), ["+1", "+2"])
    reordered = send_fingerprint(_send_request(["+2", "+1"]), ["+2", "+1"])
    assert ordered == reordered


def test_send_fingerprint_changes_with_content() -> None:
    a = send_fingerprint(_send_request(["+1"], "hello"), ["+1"])
    b = send_fingerprint(_send_request(["+1"], "goodbye"), ["+1"])
    assert a != b


# --- error taxonomy -------------------------------------------------------


def test_error_taxonomy_is_stable_and_unique() -> None:
    codes = [error.code for error in MESSAGING_ERRORS]
    assert len(codes) == len(set(codes))
    for error in MESSAGING_ERRORS:
        assert error.code.startswith("NXS_MSG_")
        assert issubclass(error, NxsError)
        assert 400 <= error.status <= 599
    retryable = {e.code for e in MESSAGING_ERRORS if e.retryable}
    assert "NXS_MSG_RATE_LIMITED" in retryable
    assert "NXS_MSG_SEND_IN_PROGRESS" in retryable
    assert "NXS_MSG_TIMEOUT" not in retryable  # ambiguous — never auto-retried
    assert "NXS_MSG_SIGNATURE_INVALID" not in retryable


# --- WhatsApp provider normalization ------------------------------------------


def _wa_account():  # type: ignore[no-untyped-def]
    import datetime as dt
    import uuid

    from nexus_ai.messaging.entities import AccountStatus, MessagingAccount

    return MessagingAccount(
        id=uuid.uuid7(),
        organization_id=uuid.uuid7(),
        channel=MessageChannel.WHATSAPP,
        provider="meta_cloud",
        slug="wa",
        external_account_id="phone-number-id-1",
        sender_identity="+14155550100",
        credential_ref="msg:wa:x",
        status=AccountStatus.ACTIVE,
        configuration={},
        webhook_token="tok",
        created_at=dt.datetime.now(dt.UTC),
        updated_at=dt.datetime.now(dt.UTC),
    )


def test_whatsapp_parses_inbound_and_status() -> None:
    body = json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"display_phone_number": "14155550100"},
                                "messages": [
                                    {
                                        "from": "14155550142",
                                        "id": "wamid.INBOUND1",
                                        "timestamp": "1700000000",
                                        "type": "text",
                                        "text": {"body": "hello there"},
                                    }
                                ],
                                "statuses": [
                                    {
                                        "id": "wamid.OUT1",
                                        "status": "delivered",
                                        "timestamp": "1700000005",
                                        "recipient_id": "14155550142",
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
    ).encode()
    parsed = WhatsAppProvider().parse_webhook(_wa_account(), WebhookContext("POST", {}, {}, body))
    assert len(parsed.inbound) == 1
    inbound = parsed.inbound[0]
    assert inbound.provider_message_id == "wamid.INBOUND1"
    assert inbound.sender_raw == "+14155550142"
    assert inbound.content.text == "hello there"
    assert len(parsed.statuses) == 1
    assert parsed.statuses[0].status is MessageStatus.DELIVERED
    # the sender/business pair is deterministic regardless of direction
    assert inbound.external_thread_hint == "+14155550100-+14155550142"


async def test_whatsapp_get_challenge_requires_matching_verify_token() -> None:
    from nexus_ai.integrations.credentials import CredentialType, SecretMaterial
    from nexus_ai.messaging.errors import MessagingChallengeFailedError

    account = _wa_account()
    secret = SecretMaterial(CredentialType.PROVIDER_SECRET_SET, {"verify_token": "s3cret"})
    provider = WhatsAppProvider()
    ok_ctx = WebhookContext(
        "GET",
        {},
        {"hub.mode": "subscribe", "hub.verify_token": "s3cret", "hub.challenge": "12345"},
        b"",
    )
    await provider.verify_webhook(account, ok_ctx, secret, timestamp_tolerance_seconds=300)
    assert provider.webhook_challenge(account, ok_ctx) == "12345"
    bad_ctx = WebhookContext("GET", {}, {"hub.mode": "subscribe", "hub.verify_token": "wrong"}, b"")
    with pytest.raises(MessagingChallengeFailedError):
        await provider.verify_webhook(account, bad_ctx, secret, timestamp_tolerance_seconds=300)


async def test_whatsapp_post_signature_is_verified_constant_time() -> None:
    from nexus_ai.integrations.credentials import CredentialType, SecretMaterial
    from nexus_ai.messaging.errors import MessagingSignatureInvalidError

    account = _wa_account()
    secret = SecretMaterial(CredentialType.PROVIDER_SECRET_SET, {"app_secret": "appsec"})
    body = b'{"entry": []}'
    good = hmac.new(b"appsec", body, hashlib.sha256).hexdigest()
    provider = WhatsAppProvider()
    await provider.verify_webhook(
        account,
        WebhookContext("POST", {"X-Hub-Signature-256": f"sha256={good}"}, {}, body),
        secret,
        timestamp_tolerance_seconds=300,
    )
    with pytest.raises(MessagingSignatureInvalidError):
        await provider.verify_webhook(
            account,
            WebhookContext("POST", {"X-Hub-Signature-256": "sha256=deadbeef"}, {}, body),
            secret,
            timestamp_tolerance_seconds=300,
        )
    with pytest.raises(MessagingSignatureInvalidError):
        await provider.verify_webhook(
            account,
            WebhookContext("POST", {}, {}, body),
            secret,
            timestamp_tolerance_seconds=300,
        )


def test_whatsapp_media_metadata_is_bounded_and_provider_mime_untrusted() -> None:
    body = json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": "14155550142",
                                        "id": "wamid.MEDIA1",
                                        "type": "image",
                                        "image": {
                                            "id": "media-1",
                                            "mime_type": "image/png",
                                            "caption": "look",
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    ).encode()
    parsed = WhatsAppProvider().parse_webhook(_wa_account(), WebhookContext("POST", {}, {}, body))
    media = parsed.inbound[0].content.media
    assert len(media) == 1
    assert media[0].declared_mime_type == "image/png"  # a declared HINT, not trusted
    assert parsed.inbound[0].content.content_type is MessageContentType.MEDIA


# --- Email + SMS provider normalization ------------------------------------


def test_email_parses_inbound_with_threading_keys() -> None:
    import datetime as dt
    import uuid

    from nexus_ai.messaging.entities import AccountStatus, MessagingAccount

    account = MessagingAccount(
        id=uuid.uuid7(),
        organization_id=uuid.uuid7(),
        channel=MessageChannel.EMAIL,
        provider="generic_http",
        slug="mail",
        external_account_id="stream-1",
        sender_identity="support@nexus.example",
        credential_ref="c",
        status=AccountStatus.ACTIVE,
        configuration={},
        webhook_token="t",
        created_at=dt.datetime.now(dt.UTC),
        updated_at=dt.datetime.now(dt.UTC),
    )
    body = json.dumps(
        {
            "type": "inbound",
            "from": "ada@example.com",
            "to": ["support@nexus.example"],
            "message_id": "<abc@mail>",
            "in_reply_to": "<root@mail>",
            "references": ["<root@mail>"],
            "subject": "Re: order",
            "text": "still broken",
        }
    ).encode()
    parsed = EmailProvider().parse_webhook(account, WebhookContext("POST", {}, {}, body))
    inbound = parsed.inbound[0]
    assert inbound.provider_message_id == "abc@mail"
    assert inbound.external_thread_hint == "root@mail"  # deterministic thread root
    assert inbound.email is not None and inbound.email.subject == "Re: order"


def test_sms_parses_inbound_and_normalizes_phone_pair() -> None:
    import datetime as dt
    import uuid

    from nexus_ai.messaging.entities import AccountStatus, MessagingAccount

    account = MessagingAccount(
        id=uuid.uuid7(),
        organization_id=uuid.uuid7(),
        channel=MessageChannel.SMS,
        provider="generic_http",
        slug="sms",
        external_account_id="svc-1",
        sender_identity="+14155550100",
        credential_ref="c",
        status=AccountStatus.ACTIVE,
        configuration={},
        webhook_token="t",
        created_at=dt.datetime.now(dt.UTC),
        updated_at=dt.datetime.now(dt.UTC),
    )
    body = json.dumps(
        {"from": "+14155550142", "to": "+14155550100", "message_id": "sm-1", "text": "yo"}
    ).encode()
    parsed = SmsProvider().parse_webhook(account, WebhookContext("POST", {}, {}, body))
    assert parsed.inbound[0].external_thread_hint == "+14155550100-+14155550142"
