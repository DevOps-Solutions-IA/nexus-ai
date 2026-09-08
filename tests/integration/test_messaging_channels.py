"""Messaging channels: account lifecycle + governed inbound/outbound per channel (NXS-P09).

Proves NXS-WA-001 / NXS-EMAIL-001 / NXS-SMS-001 on the NXS-P06 conversation model with a
fake provider transport (no socket).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.messaging.entities import (
    AccountStatus,
    CreateAccountRequest,
    MessageChannel,
    MessageContent,
    MessageContentType,
    MessageDirection,
    MessageStatus,
    SendMessageRequest,
    StoreAccountCredentialRequest,
)
from nexus_ai.messaging.providers.base import WebhookContext

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

_WA_SECRET = {"access_token": "wa-token", "app_secret": "wa-app-secret", "verify_token": "vt"}
_HTTP_SECRET = {"api_token": "prov-token", "webhook_secret": "prov-webhook-secret"}


async def _account(
    stack: Any, org_id: Any, channel: MessageChannel, *, sender: str, secret: dict[str, str]
) -> Any:
    provider = {
        MessageChannel.WHATSAPP: "meta_cloud",
        MessageChannel.EMAIL: "generic_http",
        MessageChannel.SMS: "generic_http",
    }[channel]
    external = {"WHATSAPP": "pnid-1", "EMAIL": "stream-1", "SMS": "svc-1"}[channel.value]
    account = await stack.service.create_account(
        org_id,
        CreateAccountRequest(
            channel=channel,
            provider=provider,
            slug=channel.value.lower(),
            external_account_id=external,
            sender_identity=sender,
        ),
    )
    await stack.service.store_account_credential(
        org_id, account.id, StoreAccountCredentialRequest(fields=secret)
    )
    return await stack.service.get_account(org_id, account.id)


def _sign(secret: str, body: bytes) -> dict[str, str]:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Messaging-Signature": f"sha256={digest}"}


def _wa_sign(secret: str, body: bytes) -> dict[str, str]:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}"}


# --- account lifecycle -----------------------------------------------------


async def test_account_crud_and_credential_lifecycle(
    messaging_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    stack = messaging_stack
    account = await stack.service.create_account(
        org.id,
        CreateAccountRequest(
            channel=MessageChannel.SMS,
            provider="generic_http",
            slug="sms-main",
            external_account_id="svc-1",
            sender_identity=" +1 415 555 0100 ",
        ),
    )
    assert account.sender_identity == "+14155550100"  # normalized
    assert account.status is AccountStatus.ACTIVE

    view = account.public_view()
    assert not view.has_credential
    assert view.receive_path.startswith("/api/v1/webhooks/messaging/generic_http/")

    await stack.service.store_account_credential(
        org.id, account.id, StoreAccountCredentialRequest(fields=_HTTP_SECRET)
    )
    refreshed = await stack.service.get_account(org.id, account.id)
    assert refreshed.credential_ref is not None
    assert refreshed.public_view().has_credential

    disabled = await stack.service.set_account_status(org.id, account.id, AccountStatus.DISABLED)
    assert disabled.status is AccountStatus.DISABLED

    await stack.service.delete_account(org.id, account.id)
    from nexus_ai.messaging.errors import MessagingAccountNotFoundError

    with pytest.raises(MessagingAccountNotFoundError):
        await stack.service.get_account(org.id, account.id)
    # the credential is gone with the account
    assert not await stack.vault.has_secret(org.id, refreshed.credential_ref)


async def test_account_rejects_bad_sender_and_unknown_provider(
    messaging_stack: Any, make_organization: Any
) -> None:
    from nexus_ai.messaging.errors import MessagingConfigInvalidError

    org = await make_organization()
    with pytest.raises(MessagingConfigInvalidError):
        await messaging_stack.service.create_account(
            org.id,
            CreateAccountRequest(
                channel=MessageChannel.EMAIL,
                provider="generic_http",
                slug="bad",
                external_account_id="x",
                sender_identity="not-an-email",
            ),
        )
    with pytest.raises(MessagingConfigInvalidError):
        await messaging_stack.service.create_account(
            org.id,
            CreateAccountRequest(
                channel=MessageChannel.SMS,
                provider="nonexistent",
                slug="bad2",
                external_account_id="x",
                sender_identity="+14155550100",
            ),
        )


# --- outbound: one governed path per channel ------------------------------


@pytest.mark.parametrize(
    ("channel", "sender", "recipient", "secret"),
    [
        (MessageChannel.WHATSAPP, "+14155550100", "+14155550142", _WA_SECRET),
        (MessageChannel.SMS, "+14155550100", "+14155550142", _HTTP_SECRET),
        (MessageChannel.EMAIL, "support@nexus.example", "ada@example.com", _HTTP_SECRET),
    ],
)
async def test_outbound_send_routes_through_provider_and_records_state(
    messaging_stack: Any,
    make_organization: Any,
    channel: MessageChannel,
    sender: str,
    recipient: str,
    secret: dict[str, str],
) -> None:
    org = await make_organization()
    stack = messaging_stack
    account = await _account(stack, org.id, channel, sender=sender, secret=secret)

    # a conversation on the right channel
    from nexus_ai.domain.customers.entities import CreateConversationRequest

    conversation, _ = await stack.conversations.open_or_resolve(
        org.id, CreateConversationRequest(channel=channel.value.lower())
    )

    stack.transport.set_response(200, {"messages": [{"id": "wamid.OUT"}], "message_id": "eml-OUT"})
    message = await stack.service.send(
        org.id,
        None,
        SendMessageRequest(
            account_id=account.id,
            conversation_id=conversation.id,
            to=({"value": recipient},),  # type: ignore[arg-type]
            content=MessageContent(content_type=MessageContentType.TEXT, text="hello"),
            subject="Hi" if channel is MessageChannel.EMAIL else None,
        ),
    )
    assert message.direction is MessageDirection.OUTBOUND
    assert message.status is MessageStatus.SENT
    assert message.provider_message_id in ("wamid.OUT", "eml-OUT")
    assert len(stack.transport.requests) == 1
    # the provider request carried the bearer token; the stored message never does
    auth = stack.transport.requests[0]["headers"].get("Authorization", "")
    assert auth.startswith("Bearer ")
    assert "token" not in message.model_dump_json()

    async with stack.database.tenant_transaction(org.id) as tenant:
        events = {
            row[0]
            for row in (
                await tenant.session.execute(
                    text("SELECT event_type FROM event_outbox WHERE event_type LIKE 'messaging.%'")
                )
            ).all()
        }
        activity = (
            await tenant.session.execute(
                text(
                    "SELECT activity_type FROM conversation_activities "
                    "WHERE conversation_id = :c ORDER BY occurred_at DESC LIMIT 1"
                ),
                {"c": conversation.id},
            )
        ).scalar_one()
    assert "messaging.message.queued" in events and "messaging.message.sent" in events
    assert activity == "message.outbound"


async def test_send_rejects_disabled_account_and_wrong_channel_conversation(
    messaging_stack: Any, make_organization: Any
) -> None:
    from nexus_ai.domain.customers.entities import CreateConversationRequest
    from nexus_ai.messaging.errors import (
        MessagingAccountDisabledError,
        MessagingConversationInvalidError,
    )

    org = await make_organization()
    stack = messaging_stack
    account = await _account(
        stack, org.id, MessageChannel.SMS, sender="+14155550100", secret=_HTTP_SECRET
    )
    wa_conversation, _ = await stack.conversations.open_or_resolve(
        org.id, CreateConversationRequest(channel="whatsapp")
    )
    with pytest.raises(MessagingConversationInvalidError):
        await stack.service.send(
            org.id,
            None,
            SendMessageRequest(
                account_id=account.id,
                conversation_id=wa_conversation.id,
                to=({"value": "+14155550142"},),  # type: ignore[arg-type]
                content=MessageContent(content_type=MessageContentType.TEXT, text="x"),
            ),
        )
    await stack.service.set_account_status(org.id, account.id, AccountStatus.DISABLED)
    sms_conversation, _ = await stack.conversations.open_or_resolve(
        org.id, CreateConversationRequest(channel="sms")
    )
    with pytest.raises(MessagingAccountDisabledError):
        await stack.service.send(
            org.id,
            None,
            SendMessageRequest(
                account_id=account.id,
                conversation_id=sms_conversation.id,
                to=({"value": "+14155550142"},),  # type: ignore[arg-type]
                content=MessageContent(content_type=MessageContentType.TEXT, text="x"),
            ),
        )


# --- inbound: webhook -> P06 identity + conversation -> message ----------


async def test_whatsapp_inbound_webhook_resolves_customer_and_conversation(
    messaging_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    stack = messaging_stack
    account = await _account(
        stack, org.id, MessageChannel.WHATSAPP, sender="+14155550100", secret=_WA_SECRET
    )
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
                                        "id": "wamid.IN1",
                                        "timestamp": "1700000000",
                                        "type": "text",
                                        "text": {"body": "hi from a customer"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
    ).encode()
    ctx = WebhookContext("POST", _wa_sign("wa-app-secret", body), {}, body)
    result = await stack.inbound.receive("meta_cloud", account.webhook_token, ctx)
    assert result.accepted and result.received == 1

    message = await stack.service.get_message(
        org.id, __import__("uuid").UUID(result.message_ids[0])
    )
    assert message.direction is MessageDirection.INBOUND
    assert message.status is MessageStatus.RECEIVED
    assert message.customer_id is not None
    assert message.content.text == "hi from a customer"

    # P06 owns the customer + conversation; a message-inbound activity is on the timeline
    async with stack.database.tenant_transaction(org.id) as tenant:
        identity = (
            await tenant.session.execute(
                text(
                    "SELECT normalized_value FROM customer_identities "
                    "WHERE customer_id = :c AND identity_type = 'PHONE'"
                ),
                {"c": message.customer_id},
            )
        ).scalar_one()
        events = {
            row[0]
            for row in (
                await tenant.session.execute(
                    text("SELECT event_type FROM event_outbox WHERE event_type LIKE 'messaging.%'")
                )
            ).all()
        }
    assert identity == "+14155550142"
    assert "messaging.message.received" in events

    # a replayed identical webhook creates no second message
    replay = await stack.inbound.receive("meta_cloud", account.webhook_token, ctx)
    assert replay.received == 0 and replay.replayed == 1


async def test_email_inbound_threads_deterministically_by_references(
    messaging_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    stack = messaging_stack
    account = await _account(
        stack, org.id, MessageChannel.EMAIL, sender="support@nexus.example", secret=_HTTP_SECRET
    )

    def _inbound(message_id: str, references: list[str]) -> bytes:
        return json.dumps(
            {
                "type": "inbound",
                "from": "ada@example.com",
                "to": ["support@nexus.example"],
                "message_id": message_id,
                "references": references,
                "subject": "Re: ticket",
                "text": "reply body",
            }
        ).encode()

    first = _inbound("<m1@mail>", [])
    await stack.inbound.receive(
        "generic_http",
        account.webhook_token,
        WebhookContext("POST", _sign("prov-webhook-secret", first), {}, first),
    )
    second = _inbound("<m2@mail>", ["<m1@mail>"])
    r2 = await stack.inbound.receive(
        "generic_http",
        account.webhook_token,
        WebhookContext("POST", _sign("prov-webhook-secret", second), {}, second),
    )
    msg2 = await stack.service.get_message(org.id, __import__("uuid").UUID(r2.message_ids[0]))
    async with stack.database.tenant_transaction(org.id) as tenant:
        conversations = (
            await tenant.session.execute(
                text("SELECT count(*) FROM conversations WHERE channel = 'email'")
            )
        ).scalar_one()
    assert conversations == 1  # both emails threaded to one conversation
    assert msg2.channel is MessageChannel.EMAIL


async def test_delivery_status_callback_advances_message_state(
    messaging_stack: Any, make_organization: Any
) -> None:
    from nexus_ai.domain.customers.entities import CreateConversationRequest

    org = await make_organization()
    stack = messaging_stack
    account = await _account(
        stack, org.id, MessageChannel.SMS, sender="+14155550100", secret=_HTTP_SECRET
    )
    conversation, _ = await stack.conversations.open_or_resolve(
        org.id, CreateConversationRequest(channel="sms")
    )
    stack.transport.set_response(200, {"message_id": "sm-OUT-1"})
    message = await stack.service.send(
        org.id,
        None,
        SendMessageRequest(
            account_id=account.id,
            conversation_id=conversation.id,
            to=({"value": "+14155550142"},),  # type: ignore[arg-type]
            content=MessageContent(content_type=MessageContentType.TEXT, text="hi"),
        ),
    )

    def _status(status: str) -> bytes:
        return json.dumps({"message_id": "sm-OUT-1", "status": status}).encode()

    for status_name in ("delivered", "delivered"):  # second is a duplicate
        payload = _status(status_name)
        await stack.inbound.receive(
            "generic_http",
            account.webhook_token,
            WebhookContext("POST", _sign("prov-webhook-secret", payload), {}, payload),
        )
    updated = await stack.service.get_message(org.id, message.id)
    assert updated.status is MessageStatus.DELIVERED
    assert updated.delivered_at is not None

    # an out-of-order 'sent' after 'delivered' does not regress the state
    late = _status("sent")
    await stack.inbound.receive(
        "generic_http",
        account.webhook_token,
        WebhookContext("POST", _sign("prov-webhook-secret", late), {}, late),
    )
    assert (await stack.service.get_message(org.id, message.id)).status is MessageStatus.DELIVERED


async def test_email_outbound_carries_threading_headers_and_status_callbacks(
    messaging_stack: Any, make_organization: Any
) -> None:
    from nexus_ai.domain.customers.entities import CreateConversationRequest

    org = await make_organization()
    stack = messaging_stack
    account = await _account(
        stack, org.id, MessageChannel.EMAIL, sender="support@nexus.example", secret=_HTTP_SECRET
    )
    conversation, _ = await stack.conversations.open_or_resolve(
        org.id, CreateConversationRequest(channel="email")
    )
    stack.transport.set_handler(lambda e: (200, {"message_id": "eml-OUT-1"}))
    message = await stack.service.send(
        org.id,
        None,
        SendMessageRequest(
            account_id=account.id,
            conversation_id=conversation.id,
            to=({"value": "ada@example.com"},),  # type: ignore[arg-type]
            content=MessageContent(
                content_type=MessageContentType.HTML, text="hi", html="<p>hi</p>"
            ),
            subject="Weekly digest",
        ),
    )
    body = stack.transport.requests[-1]["json"]
    assert body["from"] == "support@nexus.example"
    assert body["subject"] == "Weekly digest"
    assert body["html"] == "<p>hi</p>"

    # a provider 'bounced' status maps to canonical FAILED with an error code
    bounce = json.dumps(
        {"message_id": "eml-OUT-1", "status": "bounced", "reason": "mailbox_full"}
    ).encode()
    await stack.inbound.receive(
        "generic_http",
        account.webhook_token,
        WebhookContext("POST", _sign("prov-webhook-secret", bounce), {}, body=bounce),
    )
    updated = await stack.service.get_message(org.id, message.id)
    assert updated.status is MessageStatus.FAILED
    assert updated.failed_at is not None


async def test_whatsapp_get_challenge_through_the_pipeline(
    messaging_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    stack = messaging_stack
    account = await _account(
        stack, org.id, MessageChannel.WHATSAPP, sender="+14155550100", secret=_WA_SECRET
    )
    ctx = WebhookContext(
        "GET",
        {},
        {"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "echo-me-123"},
        b"",
    )
    result = await stack.inbound.receive("meta_cloud", account.webhook_token, ctx)
    assert result.challenge == "echo-me-123"


async def test_account_update_and_known_providers(
    messaging_stack: Any, make_organization: Any
) -> None:
    from nexus_ai.messaging.entities import UpdateAccountRequest
    from nexus_ai.messaging.providers.registry import known_providers

    org = await make_organization()
    stack = messaging_stack
    account = await _account(
        stack, org.id, MessageChannel.SMS, sender="+14155550100", secret=_HTTP_SECRET
    )
    updated = await stack.service.update_account(
        org.id, account.id, UpdateAccountRequest(configuration={"default_country": "1"})
    )
    assert updated.configuration["default_country"] == "1"
    # a no-op update returns the account unchanged
    same = await stack.service.update_account(org.id, account.id, UpdateAccountRequest())
    assert same.id == account.id
    assert ("SMS", "generic_http") in known_providers()
    await stack.service.delete_account_credential(org.id, account.id)
    assert (await stack.service.get_account(org.id, account.id)).credential_ref is None
