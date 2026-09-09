"""Messaging channels resilience — provider failure mapping, no unsafe retry (NXS-P09)."""

from __future__ import annotations

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
from nexus_ai.messaging.errors import (
    MessagingDeliveryFailedError,
    MessagingProviderError,
    MessagingRateLimitedError,
    MessagingTimeoutError,
)
from nexus_ai.messaging.providers.base import TransportError

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _ready(stack: Any, org_id: Any) -> tuple[Any, Any]:
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
        org_id, account.id, StoreAccountCredentialRequest(fields={"api_token": "t"})
    )
    account = await stack.service.get_account(org_id, account.id)
    conversation, _ = await stack.conversations.open_or_resolve(
        org_id, CreateConversationRequest(channel="sms")
    )
    return account, conversation


def _request(account: Any, conversation: Any, *, key: str | None = None) -> SendMessageRequest:
    return SendMessageRequest(
        account_id=account.id,
        conversation_id=conversation.id,
        to=({"value": "+14155550142"},),  # type: ignore[arg-type]
        content=MessageContent(content_type=MessageContentType.TEXT, text="hi"),
        idempotency_key=key,
    )


async def test_provider_timeout_is_ambiguous_and_never_retried(
    messaging_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    stack = messaging_stack
    account, conversation = await _ready(stack, org.id)
    stack.transport.set_handler(lambda e: TransportError("timed out", timeout=True))
    with pytest.raises(MessagingTimeoutError):
        await stack.service.send(
            org.id, None, _request(account, conversation, key="timeout-key-01")
        )
    assert len(stack.transport.requests) == 1  # exactly one attempt, no blind retry

    async with stack.database.tenant_transaction(org.id) as tenant:
        message_status, error_code = (
            await tenant.session.execute(
                text(
                    "SELECT status, error_code FROM messaging_messages "
                    "ORDER BY created_at DESC LIMIT 1"
                )
            )
        ).one()
        idem_status = (
            await tenant.session.execute(
                text(
                    "SELECT status FROM messaging_send_idempotency "
                    "WHERE idempotency_key = 'timeout-key-01'"
                )
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
    assert message_status == "FAILED" and error_code == "NXS_MSG_TIMEOUT"
    assert idem_status == "FAILED"
    assert "messaging.message.failed" in events

    # a retry with the SAME key does not silently re-send — it fails deterministically
    with pytest.raises(MessagingDeliveryFailedError):
        await stack.service.send(
            org.id, None, _request(account, conversation, key="timeout-key-01")
        )
    assert len(stack.transport.requests) == 1


async def test_provider_rate_limit_maps_to_tool_rate_limited(
    messaging_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    stack = messaging_stack
    account, conversation = await _ready(stack, org.id)
    stack.transport.set_handler(lambda e: (429, {"error_code": "rate_limited"}))
    with pytest.raises(MessagingRateLimitedError):
        await stack.service.send(org.id, None, _request(account, conversation))


async def test_provider_5xx_and_malformed_response_fail_the_message(
    messaging_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    stack = messaging_stack
    account, conversation = await _ready(stack, org.id)

    stack.transport.set_handler(lambda e: (503, {"error_code": "unavailable"}))
    with pytest.raises(MessagingProviderError) as excinfo:
        await stack.service.send(org.id, None, _request(account, conversation))
    assert excinfo.value.extensions.get("provider_status") == 503

    stack.transport.set_handler(lambda e: (200, b"<html>not json</html>"))
    with pytest.raises(MessagingProviderError):
        await stack.service.send(org.id, None, _request(account, conversation))

    stack.transport.set_handler(lambda e: (200, {"no_message_id": True}))
    with pytest.raises(MessagingProviderError):
        await stack.service.send(org.id, None, _request(account, conversation))

    async with stack.database.tenant_transaction(org.id) as tenant:
        failed = (
            await tenant.session.execute(
                text("SELECT count(*) FROM messaging_messages WHERE status = 'FAILED'")
            )
        ).scalar_one()
    assert failed == 3


async def test_connection_failure_is_a_retryable_provider_error(
    messaging_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    stack = messaging_stack
    account, conversation = await _ready(stack, org.id)
    stack.transport.set_handler(lambda e: TransportError("connection refused", connect=True))
    with pytest.raises(MessagingProviderError) as excinfo:
        await stack.service.send(org.id, None, _request(account, conversation))
    assert excinfo.value.retryable is False  # a connect failure is not safe to blind-retry


async def test_message_and_event_persist_atomically(
    messaging_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    stack = messaging_stack
    account, conversation = await _ready(stack, org.id)
    stack.transport.set_handler(lambda e: (200, {"message_id": "sm-ATOMIC"}))
    message = await stack.service.send(org.id, None, _request(account, conversation))
    assert message.status is MessageStatus.SENT
    async with stack.database.tenant_transaction(org.id) as tenant:
        message_count = (
            await tenant.session.execute(text("SELECT count(*) FROM messaging_messages"))
        ).scalar_one()
        queued_events = (
            await tenant.session.execute(
                text(
                    "SELECT count(*) FROM event_outbox "
                    "WHERE event_type = 'messaging.message.queued'"
                )
            )
        ).scalar_one()
    # exactly one message and its queued event (both committed in the same transaction)
    assert message_count == 1 and queued_events == 1
