"""Delivery-callback crash consistency (NXS-P09 audit corrective B).

The receipt claim + monotonic state fold + P04 event commit ATOMICALLY in one tenant
transaction. Any failure rolls back the receipt too, so a provider retry reprocesses
safely — no lost DELIVERED / READ / FAILED.
"""

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
    MessageStatus,
    SendMessageRequest,
    StoreAccountCredentialRequest,
)
from nexus_ai.messaging.providers.base import WebhookContext

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _ready(
    stack: Any, org_id: Any, channel: MessageChannel = MessageChannel.SMS
) -> tuple[Any, Any]:
    is_sms = channel is MessageChannel.SMS
    sender = "+14155550100" if is_sms else "support@nexus.example"
    recipient = "+14155550142" if is_sms else "ada@example.com"
    account = await stack.service.create_account(
        org_id,
        CreateAccountRequest(
            channel=channel,
            provider="generic_http",
            slug=f"{channel.value.lower()}-consistency",
            external_account_id=f"svc-{channel.value.lower()}",
            sender_identity=sender,
        ),
    )
    await stack.service.store_account_credential(
        org_id,
        account.id,
        StoreAccountCredentialRequest(fields={"api_token": "t", "webhook_secret": "s"}),
    )
    account = await stack.service.get_account(org_id, account.id)
    stack.transport.set_handler(lambda e: (200, {"message_id": "sm-OUT-1"}))
    conversation, _ = await stack.conversations.open_or_resolve(
        org_id, CreateConversationRequest(channel=channel.value.lower())
    )
    message = await stack.service.send(
        org_id,
        None,
        SendMessageRequest(
            account_id=account.id,
            conversation_id=conversation.id,
            to=({"value": recipient},),  # type: ignore[arg-type]
            content=MessageContent(content_type=MessageContentType.TEXT, text="hi"),
        ),
    )
    return account, message


def _status(status: str, message_id: str = "sm-OUT-1") -> tuple[bytes, dict[str, str]]:
    body = json.dumps({"message_id": message_id, "status": status}).encode()
    ts = str(int(time.time()))
    digest = hmac.new(b"s", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return body, {"X-Messaging-Signature": f"sha256={digest}", "X-Messaging-Timestamp": ts}


async def _deliver(stack: Any, account: Any, status: str) -> Any:
    body, headers = _status(status)
    return await stack.inbound.receive(
        "generic_http", account.webhook_token, WebhookContext("POST", headers, {}, body)
    )


async def _receipt_count(stack: Any, org_id: Any) -> int:
    async with stack.database.tenant_transaction(org_id) as tenant:
        return (
            await tenant.session.execute(text("SELECT count(*) FROM messaging_inbound_receipts"))
        ).scalar_one()


async def test_duplicate_status_callback_is_one_effective_transition(
    messaging_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    stack = messaging_stack
    account, message = await _ready(stack, org.id)
    r1 = await _deliver(stack, account, "delivered")
    r2 = await _deliver(stack, account, "delivered")  # identical duplicate
    assert r1.status_updates == 1 and r2.status_updates == 0 and r2.replayed == 1
    final = await stack.service.get_message(org.id, message.id)
    assert final.status is MessageStatus.DELIVERED
    async with stack.database.tenant_transaction(org.id) as tenant:
        events = (
            await tenant.session.execute(
                text(
                    "SELECT count(*) FROM event_outbox "
                    "WHERE event_type = 'messaging.message.delivered'"
                )
            )
        ).scalar_one()
    assert events == 1


async def test_db_failure_after_receipt_claim_rolls_the_receipt_back(
    messaging_stack: Any, make_organization: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = await make_organization()
    stack = messaging_stack
    account, message = await _ready(stack, org.id)
    before = await _receipt_count(stack, org.id)

    from nexus_ai.domain.messaging import repository as repo_module

    calls = {"n": 0}
    real_apply = repo_module.MessagingMessageRepository.apply

    async def _flaky_apply(self, message_id, changes):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated DB failure after the receipt claim")
        return await real_apply(self, message_id, changes)

    monkeypatch.setattr(repo_module.MessagingMessageRepository, "apply", _flaky_apply)

    with pytest.raises(RuntimeError):
        await _deliver(stack, account, "delivered")
    # 3. the receipt rolled back with the failed transaction
    assert await _receipt_count(stack, org.id) == before
    assert (await stack.service.get_message(org.id, message.id)).status is MessageStatus.SENT

    # 5. a provider retry after the failure now succeeds
    monkeypatch.undo()
    retry = await _deliver(stack, account, "delivered")
    assert retry.status_updates == 1
    assert (await stack.service.get_message(org.id, message.id)).status is MessageStatus.DELIVERED


async def test_event_enqueue_failure_rolls_receipt_and_message_transition_back(
    messaging_stack: Any, make_organization: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = await make_organization()
    stack = messaging_stack
    account, message = await _ready(stack, org.id)
    before = await _receipt_count(stack, org.id)

    from nexus_ai.messaging.webhooks import InboundMessagingService

    calls = {"n": 0}
    real = InboundMessagingService._enqueue_status_event

    async def _flaky_enqueue(self, session, organization_id, message_arg, status):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated event enqueue failure")
        return await real(self, session, organization_id, message_arg, status)

    monkeypatch.setattr(InboundMessagingService, "_enqueue_status_event", _flaky_enqueue)

    with pytest.raises(RuntimeError):
        await _deliver(stack, account, "delivered")
    # 4. receipt + message transition both rolled back
    assert await _receipt_count(stack, org.id) == before
    assert (await stack.service.get_message(org.id, message.id)).status is MessageStatus.SENT

    monkeypatch.undo()
    await _deliver(stack, account, "delivered")
    assert (await stack.service.get_message(org.id, message.id)).status is MessageStatus.DELIVERED


async def test_no_lost_terminal_callback_after_retry(
    messaging_stack: Any, make_organization: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 6. a DELIVERED then a READ callback both land after a transient failure (Email has READ)
    org = await make_organization()
    stack = messaging_stack
    account, message = await _ready(stack, org.id, MessageChannel.EMAIL)

    from nexus_ai.domain.messaging import repository as repo_module

    fail = {"armed": True}
    real_apply = repo_module.MessagingMessageRepository.apply

    async def _apply(self, message_id, changes):  # type: ignore[no-untyped-def]
        if fail["armed"]:
            fail["armed"] = False
            raise RuntimeError("transient")
        return await real_apply(self, message_id, changes)

    monkeypatch.setattr(repo_module.MessagingMessageRepository, "apply", _apply)
    with pytest.raises(RuntimeError):
        await _deliver(stack, account, "delivered")
    # retry DELIVERED then READ — nothing lost
    assert (await _deliver(stack, account, "delivered")).status_updates == 1
    assert (await _deliver(stack, account, "read")).status_updates == 1
    final = await stack.service.get_message(org.id, message.id)
    assert final.status is MessageStatus.READ
    assert final.delivered_at is not None and final.read_at is not None
