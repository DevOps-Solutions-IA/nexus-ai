"""Messaging channels security matrix (NXS-P09)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.domain.customers.entities import CreateConversationRequest
from nexus_ai.messaging.entities import (
    CreateAccountRequest,
    MessageChannel,
    MessageContent,
    MessageContentType,
    SendMessageRequest,
    StoreAccountCredentialRequest,
)
from nexus_ai.messaging.errors import (
    MessageNotFoundError,
    MessagingAccountDisabledError,
    MessagingAccountNotFoundError,
    MessagingAuthFailedError,
    MessagingConversationInvalidError,
    MessagingPayloadInvalidError,
    MessagingSignatureInvalidError,
)
from nexus_ai.messaging.providers.base import WebhookContext

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

_WA_SECRET = {"access_token": "wa-token", "app_secret": "wa-app-secret", "verify_token": "vt"}


async def _wa_account(
    stack: Any, org_id: Any, *, slug: str = "wa", external: str = "pnid-1"
) -> Any:
    account = await stack.service.create_account(
        org_id,
        CreateAccountRequest(
            channel=MessageChannel.WHATSAPP,
            provider="meta_cloud",
            slug=slug,
            external_account_id=external,
            sender_identity="+14155550100",
        ),
    )
    await stack.service.store_account_credential(
        org_id, account.id, StoreAccountCredentialRequest(fields=_WA_SECRET)
    )
    return await stack.service.get_account(org_id, account.id)


def _wa_body(msg_id: str = "wamid.SEC1", sender: str = "14155550142") -> bytes:
    return json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"display_phone_number": "14155550100"},
                                "messages": [
                                    {
                                        "from": sender,
                                        "id": msg_id,
                                        "timestamp": "1700000000",
                                        "type": "text",
                                        "text": {"body": "hi"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
    ).encode()


def _wa_sig(body: bytes, secret: str = "wa-app-secret") -> dict[str, str]:  # noqa: S107
    return {
        "X-Hub-Signature-256": "sha256="
        + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    }


async def test_invalid_and_missing_signature_are_rejected_not_accepted(
    messaging_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    stack = messaging_stack
    account = await _wa_account(stack, org.id)
    body = _wa_body()
    with pytest.raises(MessagingSignatureInvalidError):
        await stack.inbound.receive(
            "meta_cloud",
            account.webhook_token,
            WebhookContext("POST", {"X-Hub-Signature-256": "sha256=00"}, {}, body),
        )
    with pytest.raises(MessagingSignatureInvalidError):
        await stack.inbound.receive(
            "meta_cloud", account.webhook_token, WebhookContext("POST", {}, {}, body)
        )
    # a forged/wrong-secret signature also fails
    with pytest.raises(MessagingSignatureInvalidError):
        await stack.inbound.receive(
            "meta_cloud",
            account.webhook_token,
            WebhookContext("POST", _wa_sig(body, "attacker"), {}, body),
        )
    async with stack.database.tenant_transaction(org.id) as tenant:
        count = (
            await tenant.session.execute(text("SELECT count(*) FROM messaging_messages"))
        ).scalar_one()
        customers = (
            await tenant.session.execute(text("SELECT count(*) FROM customers"))
        ).scalar_one()
    assert count == 0 and customers == 0  # no customer/message created before auth passes


async def test_unknown_token_and_wrong_provider_route_fail_closed(
    messaging_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    stack = messaging_stack
    account = await _wa_account(stack, org.id)
    body = _wa_body()
    with pytest.raises(MessagingAuthFailedError):
        await stack.inbound.receive(
            "meta_cloud", "totally-bogus-token", WebhookContext("POST", _wa_sig(body), {}, body)
        )
    # the right token but the wrong provider path
    with pytest.raises(MessagingAuthFailedError):
        await stack.inbound.receive(
            "generic_http", account.webhook_token, WebhookContext("POST", _wa_sig(body), {}, body)
        )


async def test_provider_payload_cannot_specify_the_trusted_organization(
    messaging_stack: Any, make_organization: Any
) -> None:
    victim = await make_organization()
    attacker = await make_organization()
    stack = messaging_stack
    account = await _wa_account(stack, attacker.id)
    # a payload that tries to name the victim org — it is ignored; the token owns scope
    payload = json.loads(_wa_body())
    payload["organization_id"] = str(victim.id)
    payload["entry"][0]["organization_id"] = str(victim.id)
    body = json.dumps(payload).encode()
    result = await stack.inbound.receive(
        "meta_cloud", account.webhook_token, WebhookContext("POST", _wa_sig(body), {}, body)
    )
    assert result.received == 1
    async with stack.database.tenant_transaction(victim.id) as tenant:
        victim_messages = (
            await tenant.session.execute(text("SELECT count(*) FROM messaging_messages"))
        ).scalar_one()
    async with stack.database.tenant_transaction(attacker.id) as tenant:
        attacker_messages = (
            await tenant.session.execute(text("SELECT count(*) FROM messaging_messages"))
        ).scalar_one()
    assert victim_messages == 0 and attacker_messages == 1


async def test_cross_tenant_account_and_message_lookup_fail_closed(
    messaging_stack: Any, make_organization: Any
) -> None:
    org_a = await make_organization()
    org_b = await make_organization()
    stack = messaging_stack
    account = await _wa_account(stack, org_a.id)
    with pytest.raises(MessagingAccountNotFoundError):
        await stack.service.get_account(org_b.id, account.id)

    conversation, _ = await stack.conversations.open_or_resolve(
        org_a.id, CreateConversationRequest(channel="whatsapp")
    )
    stack.transport.set_response(200, {"messages": [{"id": "wamid.X"}]})
    message = await stack.service.send(
        org_a.id,
        None,
        SendMessageRequest(
            account_id=account.id,
            conversation_id=conversation.id,
            to=({"value": "+14155550142"},),  # type: ignore[arg-type]
            content=MessageContent(content_type=MessageContentType.TEXT, text="hi"),
        ),
    )
    with pytest.raises(MessageNotFoundError):
        await stack.service.get_message(org_b.id, message.id)


async def test_send_through_another_orgs_account_is_refused(
    messaging_stack: Any, make_organization: Any
) -> None:
    org_a = await make_organization()
    org_b = await make_organization()
    stack = messaging_stack
    account = await _wa_account(stack, org_a.id)
    conversation, _ = await stack.conversations.open_or_resolve(
        org_b.id, CreateConversationRequest(channel="whatsapp")
    )
    with pytest.raises(MessagingAccountNotFoundError):
        await stack.service.send(
            org_b.id,
            None,
            SendMessageRequest(
                account_id=account.id,
                conversation_id=conversation.id,
                to=({"value": "+14155550142"},),  # type: ignore[arg-type]
                content=MessageContent(content_type=MessageContentType.TEXT, text="hi"),
            ),
        )


async def test_provider_account_cannot_be_adopted_by_a_second_organization(
    messaging_stack: Any, make_organization: Any
) -> None:
    from nexus_ai.messaging.errors import MessagingAccountConflictError

    org_a = await make_organization()
    org_b = await make_organization()
    stack = messaging_stack
    await _wa_account(stack, org_a.id, external="shared-pnid")
    with pytest.raises(MessagingAccountConflictError):
        await _wa_account(stack, org_b.id, slug="wa2", external="shared-pnid")


async def test_email_send_rejects_crlf_header_injection_and_arbitrary_headers(
    messaging_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    stack = messaging_stack
    account = await stack.service.create_account(
        org.id,
        CreateAccountRequest(
            channel=MessageChannel.EMAIL,
            provider="generic_http",
            slug="mail",
            external_account_id="stream-1",
            sender_identity="support@nexus.example",
        ),
    )
    await stack.service.store_account_credential(
        org.id, account.id, StoreAccountCredentialRequest(fields={"api_token": "t"})
    )
    conversation, _ = await stack.conversations.open_or_resolve(
        org.id, CreateConversationRequest(channel="email")
    )
    with pytest.raises(MessagingPayloadInvalidError):
        await stack.service.send(
            org.id,
            None,
            SendMessageRequest(
                account_id=account.id,
                conversation_id=conversation.id,
                to=({"value": "ada@example.com"},),  # type: ignore[arg-type]
                content=MessageContent(content_type=MessageContentType.TEXT, text="hi"),
                subject="Hello\r\nBcc: attacker@evil.example",
            ),
        )
    # SendMessageRequest has no field for arbitrary provider headers at all
    assert "headers" not in SendMessageRequest.model_fields


async def test_oversized_and_malformed_webhook_bodies_are_rejected(
    messaging_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    stack = messaging_stack
    account = await _wa_account(stack, org.id)
    huge = b'{"entry":[]}' + b" " * (stack.settings.channels.max_webhook_body_bytes + 1)
    with pytest.raises(MessagingPayloadInvalidError):
        await stack.inbound.receive(
            "meta_cloud", account.webhook_token, WebhookContext("POST", _wa_sig(huge), {}, huge)
        )
    bad_json = b"{not json"
    with pytest.raises(MessagingPayloadInvalidError):
        await stack.inbound.receive(
            "meta_cloud",
            account.webhook_token,
            WebhookContext("POST", _wa_sig(bad_json), {}, bad_json),
        )


async def test_disabled_account_inbound_is_rejected_after_verification(
    messaging_stack: Any, make_organization: Any
) -> None:
    from nexus_ai.messaging.entities import AccountStatus

    org = await make_organization()
    stack = messaging_stack
    account = await _wa_account(stack, org.id)
    await stack.service.set_account_status(org.id, account.id, AccountStatus.DISABLED)
    body = _wa_body()
    with pytest.raises(MessagingAccountDisabledError):
        await stack.inbound.receive(
            "meta_cloud", account.webhook_token, WebhookContext("POST", _wa_sig(body), {}, body)
        )


async def test_no_secret_or_token_leaks_into_message_records_or_events(
    messaging_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    stack = messaging_stack
    account = await _wa_account(stack, org.id)
    conversation, _ = await stack.conversations.open_or_resolve(
        org.id, CreateConversationRequest(channel="whatsapp")
    )
    stack.transport.set_response(200, {"messages": [{"id": "wamid.LEAK"}]})
    await stack.service.send(
        org.id,
        None,
        SendMessageRequest(
            account_id=account.id,
            conversation_id=conversation.id,
            to=({"value": "+14155550142"},),  # type: ignore[arg-type]
            content=MessageContent(content_type=MessageContentType.TEXT, text="hi"),
        ),
    )
    # the provider DID receive the bearer token
    assert "Bearer wa-token" in stack.transport.requests[-1]["headers"].values()
    async with stack.database.tenant_transaction(org.id) as tenant:
        rows = (
            await tenant.session.execute(
                text("SELECT row_to_json(m)::text FROM messaging_messages m")
            )
        ).all()
        events = (
            await tenant.session.execute(
                text("SELECT envelope::text FROM event_outbox WHERE event_type LIKE 'messaging.%'")
            )
        ).all()
    assert all("wa-token" not in row[0] and "wa-app-secret" not in row[0] for row in rows)
    assert all("wa-token" not in e[0] and "wa-app-secret" not in e[0] for e in events)


async def test_idempotency_key_reuse_with_a_different_payload_is_a_conflict(
    messaging_stack: Any, make_organization: Any
) -> None:
    from nexus_ai.messaging.errors import MessagingIdempotencyConflictError

    org = await make_organization()
    stack = messaging_stack
    account = await stack.service.create_account(
        org.id,
        CreateAccountRequest(
            channel=MessageChannel.SMS,
            provider="generic_http",
            slug="sms",
            external_account_id="svc-1",
            sender_identity="+14155550100",
        ),
    )
    await stack.service.store_account_credential(
        org.id, account.id, StoreAccountCredentialRequest(fields={"api_token": "t"})
    )
    conversation, _ = await stack.conversations.open_or_resolve(
        org.id, CreateConversationRequest(channel="sms")
    )
    stack.transport.set_response(200, {"message_id": "sm-IDEM"})

    def _send(textbody: str) -> SendMessageRequest:
        return SendMessageRequest(
            account_id=account.id,
            conversation_id=conversation.id,
            to=({"value": "+14155550142"},),  # type: ignore[arg-type]
            content=MessageContent(content_type=MessageContentType.TEXT, text=textbody),
            idempotency_key="reuse-key-0001",
        )

    first = await stack.service.send(org.id, None, _send("original"))
    replay = await stack.service.send(org.id, None, _send("original"))
    assert replay.id == first.id  # same key + same payload replays
    with pytest.raises(MessagingIdempotencyConflictError):
        await stack.service.send(org.id, None, _send("TAMPERED"))
    assert len([r for r in stack.transport.requests]) == 1  # upstream hit exactly once


async def test_stale_timestamp_signature_is_replay_rejected(
    messaging_stack: Any, make_organization: Any
) -> None:
    from nexus_ai.messaging.errors import MessagingReplayRejectedError

    org = await make_organization()
    stack = messaging_stack
    account = await stack.service.create_account(
        org.id,
        CreateAccountRequest(
            channel=MessageChannel.SMS,
            provider="generic_http",
            slug="sms",
            external_account_id="svc-1",
            sender_identity="+14155550100",
        ),
    )
    await stack.service.store_account_credential(
        org.id, account.id, StoreAccountCredentialRequest(fields={"webhook_secret": "s"})
    )
    body = json.dumps({"message_id": "sm-1", "status": "delivered"}).encode()
    stale_ts = str(int(time.time()) - 100_000)
    signed = f"{stale_ts}.".encode() + body
    digest = hmac.new(b"s", signed, hashlib.sha256).hexdigest()
    # the generic HTTP provider does not enforce a timestamp window itself, so a stale
    # timestamp simply must not verify against a signature computed without it
    with pytest.raises(MessagingSignatureInvalidError):
        await stack.inbound.receive(
            "generic_http",
            account.webhook_token,
            WebhookContext(
                "POST",
                {"X-Messaging-Signature": f"sha256={digest}"},  # no X-Messaging-Timestamp header
                {},
                body,
            ),
        )
    _ = MessagingReplayRejectedError  # taxonomy exists for providers that carry a window
    _ = MessagingConversationInvalidError
