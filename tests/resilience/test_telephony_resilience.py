"""Telephony resilience: provider failure modes, ambiguous outbound-creation timeout,
delayed / duplicate events, transaction rollback (NXS-P11: NXS-TEL-001)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text

from nexus_ai.telephony.entities import (
    CreateAccountRequest,
    CreateCallRequest,
    HangupCallRequest,
    RegisterPhoneNumberRequest,
    TelephonyProvider,
)
from nexus_ai.telephony.errors import (
    TelephonyProviderError,
    TelephonyProviderTimeoutError,
)
from nexus_ai.telephony.providers.base import TransportError, WebhookContext
from nexus_ai.telephony.service import AmbiguousProviderTimeoutError

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

_SECRET = {"webhook_secret": "tel-secret", "ari_user": "u", "ari_password": "p"}


async def _account(stack: Any, org_id: Any) -> Any:
    account = await stack.service.create_account(
        org_id,
        CreateAccountRequest(
            provider=TelephonyProvider.FAKE,
            slug=f"tel-{uuid4().hex[:8]}",
            external_account_id=uuid4().hex,
            configuration={"default_country": "1"},
        ),
    )
    await stack.service.store_account_credential(org_id, account.id, _SECRET)
    return await stack.service.get_account(org_id, account.id)


async def _number(stack: Any, org_id: Any, account: Any) -> Any:
    n = await stack.service.register_number(
        org_id, RegisterPhoneNumberRequest(account_id=account.id, e164="+14155550100")
    )
    return await stack.service.set_number_verified(org_id, n.id, verified=True)


def _request(account: Any, number: Any, key: str | None = None) -> CreateCallRequest:
    return CreateCallRequest(
        provider_account_id=account.id,
        from_number_id=number.id,
        destination="+14155550199",
        idempotency_key=key,
    )


async def test_provider_http_failure_marks_the_call_failed(
    telephony_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(telephony_stack, org.id)
    number = await _number(telephony_stack, org.id, account)
    telephony_stack.transport.set_handler(lambda _e: (500, {"error": "boom"}))

    with pytest.raises(TelephonyProviderError):
        await telephony_stack.service.create_call(org.id, None, _request(account, number))

    async with telephony_stack.database.tenant_transaction(org.id) as tenant:
        row = (
            await tenant.session.execute(
                text("SELECT state FROM telephony_calls ORDER BY created_at DESC LIMIT 1")
            )
        ).one()
    assert row.state == "FAILED"


async def test_ambiguous_provider_timeout_never_places_a_second_call(
    telephony_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(telephony_stack, org.id)
    number = await _number(telephony_stack, org.id, account)

    def _timeout(_e: dict[str, Any]) -> TransportError:
        return TransportError("provider timed out", timeout=True)

    telephony_stack.transport.set_handler(_timeout)
    with pytest.raises((AmbiguousProviderTimeoutError, TelephonyProviderTimeoutError)):
        await telephony_stack.service.create_call(
            org.id, None, _request(account, number, key="tel-ambig-key-1")
        )
    calls_before = len(telephony_stack.transport.requests)

    # a retry under the SAME key replays the ambiguous record and NEVER places a
    # second provider call
    replay = await telephony_stack.service.create_call(
        org.id, None, _request(account, number, key="tel-ambig-key-1")
    )
    assert len(telephony_stack.transport.requests) == calls_before
    assert replay.error_code == "NXS_TELEPHONY_PROVIDER_TIMEOUT"

    async with telephony_stack.database.tenant_transaction(org.id) as tenant:
        rows = (
            await tenant.session.execute(
                text(
                    "SELECT count(*) FROM telephony_calls WHERE idempotency_key = 'tel-ambig-key-1'"
                )
            )
        ).scalar_one()
    assert rows == 1  # exactly one logical call, ambiguous but not duplicated


async def test_hangup_survives_a_provider_outage(
    telephony_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(telephony_stack, org.id)
    number = await _number(telephony_stack, org.id, account)
    call = await telephony_stack.service.create_call(org.id, None, _request(account, number))
    telephony_stack.transport.set_handler(
        lambda _e: TransportError("provider unreachable", connect=True)
    )
    hung = await telephony_stack.service.hangup_call(org.id, call.id, HangupCallRequest())
    assert hung.state.value == "ENDING"  # local terminal transition still committed


async def test_callback_for_unknown_call_is_deferred_not_dropped(
    telephony_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(telephony_stack, org.id)
    body = json.dumps(
        {"event_id": uuid4().hex, "call_id": f"pc-{uuid4().hex}", "event": "ANSWERED"}
    ).encode()
    ts = str(int(time.time()))
    digest = hmac.new(b"tel-secret", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    ctx = WebhookContext(
        method="POST",
        headers={"X-Telephony-Signature": f"sha256={digest}", "X-Telephony-Timestamp": ts},
        query={},
        body=body,
    )
    result = await telephony_stack.inbound.receive("fake", account.webhook_token, ctx)
    assert result.processed == "deferred"
    # the event was NOT claimed — the provider retry can reprocess it
    async with telephony_stack.database.tenant_transaction(org.id) as tenant:
        count = (
            await tenant.session.execute(text("SELECT count(*) FROM telephony_call_events"))
        ).scalar_one()
    assert count == 0
