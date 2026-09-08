"""Email reply-thread governance (NXS-P09 audit corrective C).

``reply_to_message_id`` is resolved under the authenticated Organization, must belong to
the same Conversation, must be Email for an Email reply, is guarded by a tenant-aware
self-reference FK, and produces real, CRLF-safe In-Reply-To / References headers.
``email.message_id_header`` is the canonical threading identifier (see ADR-0074).
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import time
import uuid
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
from nexus_ai.messaging.errors import MessagingConversationInvalidError
from nexus_ai.messaging.providers.base import WebhookContext

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

_HTTP_SECRET = {"api_token": "prov-token", "webhook_secret": "whsec"}
_ids = itertools.count(1)


async def _account(stack: Any, org_id: Any, channel: MessageChannel, sender: str) -> Any:
    account = await stack.service.create_account(
        org_id,
        CreateAccountRequest(
            channel=channel,
            provider="generic_http",
            slug=f"{channel.value.lower()}-{uuid.uuid4().hex[:6]}",
            external_account_id=uuid.uuid4().hex,
            sender_identity=sender,
        ),
    )
    await stack.service.store_account_credential(
        org_id, account.id, StoreAccountCredentialRequest(fields=_HTTP_SECRET)
    )
    return await stack.service.get_account(org_id, account.id)


def _unique_provider_id(stack: Any) -> None:
    stack.transport.set_handler(lambda e: (200, {"message_id": f"provider-{next(_ids)}"}))


def _sign(body: bytes) -> dict[str, str]:
    ts = str(int(time.time()))
    digest = hmac.new(b"whsec", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Messaging-Signature": f"sha256={digest}", "X-Messaging-Timestamp": ts}


def _send(account: Any, conversation: Any, *, reply_to: Any = None, subject: str = "Re: x") -> Any:
    return SendMessageRequest(
        account_id=account.id,
        conversation_id=conversation.id,
        to=({"value": "ada@example.com"},),  # type: ignore[arg-type]
        content=MessageContent(content_type=MessageContentType.TEXT, text="body"),
        subject=subject,
        reply_to_message_id=reply_to,
    )


async def _inbound_email(
    stack: Any, account: Any, *, message_id: str, references: list[str]
) -> Any:
    body = json.dumps(
        {
            "type": "inbound",
            "from": "ada@example.com",
            "to": ["support@nexus.example"],
            "message_id": message_id,
            "references": references,
            "subject": "Re: thread",
            "text": "a customer message",
        }
    ).encode()
    result = await stack.inbound.receive(
        "generic_http", account.webhook_token, WebhookContext("POST", _sign(body), {}, body)
    )
    return uuid.UUID(result.message_ids[0])


async def test_email_root_reply_to_inbound_and_reply_to_outbound(
    messaging_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    stack = messaging_stack
    account = await _account(stack, org.id, MessageChannel.EMAIL, "support@nexus.example")
    _unique_provider_id(stack)

    # 1. an Email ROOT send — no reply_to => no In-Reply-To / References, but a Message-ID
    conversation, _ = await stack.conversations.open_or_resolve(
        org.id, CreateConversationRequest(channel="email")
    )
    root = await stack.service.send(org.id, None, _send(account, conversation, subject="Hello"))
    root_headers = stack.transport.requests[-1]["json"]["headers"]
    assert "In-Reply-To" not in root_headers and "References" not in root_headers
    assert "@nexus.messaging>" in root_headers["Message-ID"]
    assert root.email is not None
    root_mid = root.email.message_id_header

    # 3. reply referencing the prior OUTBOUND Email (the root), same conversation
    reply_to_root = await stack.service.send(
        org.id, None, _send(account, conversation, reply_to=root.id)
    )
    h_root_reply = stack.transport.requests[-1]["json"]["headers"]
    assert h_root_reply["In-Reply-To"] == f"<{root_mid}>"
    assert root_mid in h_root_reply["References"]
    assert reply_to_root.conversation_id == conversation.id  # 6. same Conversation preserved

    # a customer-initiated inbound Email creates its own thread conversation
    inbound_id = await _inbound_email(stack, account, message_id="<customer-1@mail>", references=[])
    inbound = await stack.service.get_message(org.id, inbound_id)
    inbound_conversation = await stack.conversations.get(org.id, inbound.conversation_id)

    # 2. reply referencing the INBOUND Email, using ITS conversation
    reply_to_inbound = await stack.service.send(
        org.id, None, _send(account, inbound_conversation, reply_to=inbound_id)
    )
    h_inbound_reply = stack.transport.requests[-1]["json"]["headers"]
    # 4 + 5. emitted In-Reply-To and References verified
    assert h_inbound_reply["In-Reply-To"] == "<customer-1@mail>"
    assert "customer-1@mail" in h_inbound_reply["References"]
    assert reply_to_inbound.conversation_id == inbound.conversation_id


async def test_reply_to_cross_organization_or_wrong_conversation_is_rejected(
    messaging_stack: Any, make_organization: Any
) -> None:
    org_a = await make_organization()
    org_b = await make_organization()
    stack = messaging_stack
    account_a = await _account(stack, org_a.id, MessageChannel.EMAIL, "a@nexus.example")
    account_b = await _account(stack, org_b.id, MessageChannel.EMAIL, "b@nexus.example")
    _unique_provider_id(stack)

    conv_a1, _ = await stack.conversations.open_or_resolve(
        org_a.id, CreateConversationRequest(channel="email")
    )
    conv_a2, _ = await stack.conversations.open_or_resolve(
        org_a.id,
        CreateConversationRequest(channel="email", provider_namespace="p", external_thread_id="t2"),
    )
    parent_a = await stack.service.send(org_a.id, None, _send(account_a, conv_a1))

    conv_b, _ = await stack.conversations.open_or_resolve(
        org_b.id, CreateConversationRequest(channel="email")
    )
    # 7. cross-Organization reply_to rejected
    with pytest.raises(MessagingConversationInvalidError):
        await stack.service.send(org_b.id, None, _send(account_b, conv_b, reply_to=parent_a.id))
    # 8. wrong Conversation (same org) rejected
    with pytest.raises(MessagingConversationInvalidError):
        await stack.service.send(org_a.id, None, _send(account_a, conv_a2, reply_to=parent_a.id))
    # a non-existent parent is rejected
    with pytest.raises(MessagingConversationInvalidError):
        await stack.service.send(org_a.id, None, _send(account_a, conv_a1, reply_to=uuid.uuid7()))


async def test_sms_reply_to_cannot_exploit_email_threading(
    messaging_stack: Any, make_organization: Any
) -> None:
    # 9. an SMS / WhatsApp reply_to cannot ride the Email threading path
    org = await make_organization()
    stack = messaging_stack
    sms = await _account(stack, org.id, MessageChannel.SMS, "+14155550100")
    _unique_provider_id(stack)
    conversation, _ = await stack.conversations.open_or_resolve(
        org.id, CreateConversationRequest(channel="sms")
    )

    def _sms(reply_to: Any = None) -> SendMessageRequest:
        return SendMessageRequest(
            account_id=sms.id,
            conversation_id=conversation.id,
            to=({"value": "+14155550142"},),  # type: ignore[arg-type]
            content=MessageContent(content_type=MessageContentType.TEXT, text="x"),
            reply_to_message_id=reply_to,
        )

    root = await stack.service.send(org.id, None, _sms())
    reply = await stack.service.send(org.id, None, _sms(reply_to=root.id))
    assert reply.reply_to_message_id == root.id
    assert reply.email is None  # no email envelope on an SMS
    assert "headers" not in stack.transport.requests[-1]["json"]  # no threading headers

    # an EMAIL account may not reply to that SMS message (channel guard)
    email = await _account(stack, org.id, MessageChannel.EMAIL, "support@nexus.example")
    with pytest.raises(MessagingConversationInvalidError):
        await stack.service.send(
            org.id,
            None,
            SendMessageRequest(
                account_id=email.id,
                conversation_id=conversation.id,
                to=({"value": "ada@example.com"},),  # type: ignore[arg-type]
                content=MessageContent(content_type=MessageContentType.TEXT, text="x"),
                reply_to_message_id=root.id,
            ),
        )


async def test_database_fk_prevents_a_forged_cross_tenant_reply_relation(
    messaging_stack: Any, make_organization: Any
) -> None:
    # 10. the composite tenant-aware self-reference FK refuses a forged relation
    from sqlalchemy.exc import DBAPIError

    org_a = await make_organization()
    org_b = await make_organization()
    stack = messaging_stack
    account_a = await _account(stack, org_a.id, MessageChannel.EMAIL, "a@nexus.example")
    account_b = await _account(stack, org_b.id, MessageChannel.EMAIL, "b@nexus.example")
    _unique_provider_id(stack)
    conv_a, _ = await stack.conversations.open_or_resolve(
        org_a.id, CreateConversationRequest(channel="email")
    )
    conv_b, _ = await stack.conversations.open_or_resolve(
        org_b.id, CreateConversationRequest(channel="email")
    )
    parent_a = await stack.service.send(org_a.id, None, _send(account_a, conv_a))
    child_b = await stack.service.send(org_b.id, None, _send(account_b, conv_b))

    with pytest.raises(DBAPIError):
        async with stack.database.tenant_transaction(org_b.id) as tenant:
            await tenant.session.execute(
                text("UPDATE messaging_messages SET reply_to_message_id = :p WHERE id = :c"),
                {"p": parent_a.id, "c": child_b.id},
            )


async def test_parent_deletion_nulls_only_reply_to_and_keeps_organization(
    messaging_stack: Any, make_organization: Any
) -> None:
    """The FK deletion semantics (ON DELETE SET NULL (reply_to_message_id)):
    deleting a parent NULLs the child's reply_to_message_id and leaves
    organization_id (NOT NULL) and every other field intact — tenant isolation
    preserved, delete succeeds."""
    import asyncpg

    from tests.conftest import MIGRATION_DSN

    org = await make_organization()
    stack = messaging_stack
    account = await _account(stack, org.id, MessageChannel.EMAIL, "support@nexus.example")
    _unique_provider_id(stack)
    conversation, _ = await stack.conversations.open_or_resolve(
        org.id, CreateConversationRequest(channel="email")
    )
    parent = await stack.service.send(org.id, None, _send(account, conversation))
    child = await stack.service.send(org.id, None, _send(account, conversation, reply_to=parent.id))
    assert child.reply_to_message_id == parent.id

    # delete the parent through a real PostgreSQL transaction (owner role, tenant GUC)
    connection = await asyncpg.connect(
        MIGRATION_DSN.replace("postgresql+asyncpg://", "postgresql://", 1), timeout=10
    )
    try:
        async with connection.transaction():
            await connection.execute(
                "SELECT set_config('nxs.organization_id', $1, true)", str(org.id)
            )
            deleted = await connection.execute(
                "DELETE FROM messaging_messages WHERE id = $1", parent.id
            )
            assert deleted == "DELETE 1"  # the delete succeeds — no NOT NULL violation
            row = await connection.fetchrow(
                "SELECT organization_id, reply_to_message_id, channel, direction, status "
                "FROM messaging_messages WHERE id = $1",
                child.id,
            )
    finally:
        await connection.close()

    assert row["reply_to_message_id"] is None  # NULLed
    assert row["organization_id"] == org.id  # UNCHANGED
    assert row["channel"] == "EMAIL" and row["direction"] == "OUTBOUND"  # every other field valid

    # the child is still readable through the service in its Organization
    refreshed = await stack.service.get_message(org.id, child.id)
    assert refreshed.reply_to_message_id is None and refreshed.organization_id == org.id


async def test_reply_thread_headers_are_crlf_safe(
    messaging_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    stack = messaging_stack
    account = await _account(stack, org.id, MessageChannel.EMAIL, "support@nexus.example")
    _unique_provider_id(stack)
    # an inbound whose Message-ID carries a CRLF header-injection attempt
    inbound_id = await _inbound_email(
        stack,
        account,
        message_id="<evil@mail>\r\nBcc: attacker@evil.example",
        references=[],
    )
    inbound = await stack.service.get_message(org.id, inbound_id)
    assert inbound.email is not None
    assert "\r" not in (inbound.email.message_id_header or "")  # scrubbed at ingest
    conversation = await stack.conversations.get(org.id, inbound.conversation_id)

    reply = await stack.service.send(
        org.id, None, _send(account, conversation, reply_to=inbound_id)
    )
    headers = stack.transport.requests[-1]["json"]["headers"]
    for value in headers.values():
        assert "\r" not in value and "\n" not in value
    assert reply.email is not None
