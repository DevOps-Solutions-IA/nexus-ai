"""Messaging channels concurrency guarantees (NXS-P09)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.domain.customers.entities import CreateConversationRequest
from nexus_ai.messaging.entities import (
    CreateAccountRequest,
    MessageChannel,
    MessageContent,
    MessageContentType,
    MessageStatus,
    SendMessageRequest,
    StoreAccountCredentialRequest,
)
from nexus_ai.messaging.errors import MessagingSendInProgressError
from nexus_ai.messaging.providers.base import WebhookContext

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _sms_account(stack: Any, org_id: Any) -> Any:
    account = await stack.service.create_account(
        org_id,
        CreateAccountRequest(
            channel=MessageChannel.SMS,
            provider="generic_http",
            slug="sms",
            external_account_id="svc-1",
            sender_identity="+14155550100",
        ),
    )
    await stack.service.store_account_credential(
        org_id,
        account.id,
        StoreAccountCredentialRequest(fields={"api_token": "t", "webhook_secret": "s"}),
    )
    return await stack.service.get_account(org_id, account.id)


def _sig(body: bytes) -> dict[str, str]:
    import time

    timestamp = str(int(time.time()))
    digest = hmac.new(b"s", f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return {
        "X-Messaging-Signature": f"sha256={digest}",
        "X-Messaging-Timestamp": timestamp,
    }


async def test_concurrent_duplicate_inbound_webhook_persists_once(
    messaging_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    stack = messaging_stack
    account = await _sms_account(stack, org.id)
    body = json.dumps(
        {"from": "+14155550142", "to": "+14155550100", "message_id": "sm-DUP", "text": "hi"}
    ).encode()
    ctx = WebhookContext("POST", _sig(body), {}, body)

    results = await asyncio.gather(
        *(stack.inbound.receive("generic_http", account.webhook_token, ctx) for _ in range(8)),
        return_exceptions=True,
    )
    received = sum(getattr(r, "received", 0) for r in results if not isinstance(r, Exception))
    replayed = sum(getattr(r, "replayed", 0) for r in results if not isinstance(r, Exception))
    assert received == 1 and replayed == 7
    async with stack.database.tenant_transaction(org.id) as tenant:
        messages = (
            await tenant.session.execute(text("SELECT count(*) FROM messaging_messages"))
        ).scalar_one()
        customers = (
            await tenant.session.execute(text("SELECT count(*) FROM customers"))
        ).scalar_one()
    assert messages == 1 and customers == 1  # no duplicate message, no customer race


async def test_concurrent_outbound_same_idempotency_key_calls_provider_once(
    messaging_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    stack = messaging_stack
    account = await _sms_account(stack, org.id)
    conversation, _ = await stack.conversations.open_or_resolve(
        org.id, CreateConversationRequest(channel="sms")
    )
    hits = {"n": 0}

    def handler(entry: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        import time as _t

        hits["n"] += 1
        _t.sleep(0.05)
        return (200, {"message_id": f"sm-{hits['n']}"})

    stack.transport.set_handler(handler)
    request = SendMessageRequest(
        account_id=account.id,
        conversation_id=conversation.id,
        to=({"value": "+14155550142"},),  # type: ignore[arg-type]
        content=MessageContent(content_type=MessageContentType.TEXT, text="hi"),
        idempotency_key="race-send-0001",
    )
    results = await asyncio.gather(
        *(stack.service.send(org.id, None, request) for _ in range(6)),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    in_progress = [r for r in results if isinstance(r, MessagingSendInProgressError)]
    assert len(successes) + len(in_progress) == 6
    assert len({m.id for m in successes}) == 1  # one message
    assert hits["n"] == 1  # provider hit exactly once
    async with stack.database.tenant_transaction(org.id) as tenant:
        completed = (
            await tenant.session.execute(
                text("SELECT count(*) FROM messaging_send_idempotency WHERE status = 'COMPLETED'")
            )
        ).scalar_one()
        rows = (
            await tenant.session.execute(text("SELECT count(*) FROM messaging_messages"))
        ).scalar_one()
    assert completed == 1 and rows == 1


async def test_concurrent_delivery_status_callbacks_keep_state_valid(
    messaging_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    stack = messaging_stack
    account = await _sms_account(stack, org.id)
    conversation, _ = await stack.conversations.open_or_resolve(
        org.id, CreateConversationRequest(channel="sms")
    )
    stack.transport.set_handler(lambda e: (200, {"message_id": "sm-STATUS"}))
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

    def _status(status: str) -> Any:
        body = json.dumps({"message_id": "sm-STATUS", "status": status}).encode()
        return stack.inbound.receive(
            "generic_http", account.webhook_token, WebhookContext("POST", _sig(body), {}, body)
        )

    await asyncio.gather(
        *(_status(name) for name in ("sent", "delivered", "delivered", "sent", "delivered")),
        return_exceptions=True,
    )
    final = await stack.service.get_message(org.id, message.id)
    assert final.status in (MessageStatus.SENT, MessageStatus.DELIVERED)
    # never regressed below the highest reported non-regressive state
    assert final.status is MessageStatus.DELIVERED


async def test_concurrent_registration_of_the_same_provider_account_conflicts(
    messaging_stack: Any, make_organization: Any
) -> None:
    from nexus_ai.messaging.errors import MessagingAccountConflictError

    org = await make_organization()
    stack = messaging_stack

    async def _create(index: int) -> Any:
        return await stack.service.create_account(
            org.id,
            CreateAccountRequest(
                channel=MessageChannel.SMS,
                provider="generic_http",
                slug=f"sms-{index}",
                external_account_id="contested-svc",
                sender_identity="+14155550100",
            ),
        )

    results = await asyncio.gather(*(_create(i) for i in range(5)), return_exceptions=True)
    ok = [r for r in results if not isinstance(r, Exception)]
    conflicts = [r for r in results if isinstance(r, MessagingAccountConflictError)]
    assert len(ok) == 1 and len(conflicts) == 4
