"""Telephony account / number / call flows against the real database + event outbox with
a fake provider transport (NXS-P11: NXS-TEL-001)."""

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
    AccountStatus,
    CallDirection,
    CallState,
    CreateAccountRequest,
    CreateCallRequest,
    HangupCallRequest,
    RegisterPhoneNumberRequest,
    SendDtmfRequest,
    TelephonyProvider,
)
from nexus_ai.telephony.errors import (
    TelephonyCallNotFoundError,
    TelephonyConfigInvalidError,
    TelephonyIdempotencyConflictError,
    TelephonyInvalidStateError,
    TelephonyWebhookInvalidError,
)
from nexus_ai.telephony.providers.base import WebhookContext

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

_SECRET = {"webhook_secret": "tel-webhook-secret", "ari_user": "u", "ari_password": "p"}


async def _account(
    stack: Any, org_id: Any, *, provider: TelephonyProvider = TelephonyProvider.FAKE
) -> Any:
    account = await stack.service.create_account(
        org_id,
        CreateAccountRequest(
            provider=provider,
            slug=f"tel-{uuid4().hex[:8]}",
            external_account_id=uuid4().hex,
            configuration={"default_country": "1"},
        ),
    )
    await stack.service.store_account_credential(org_id, account.id, _SECRET)
    return await stack.service.get_account(org_id, account.id)


async def _number(
    stack: Any, org_id: Any, account: Any, *, e164: str = "+14155550100", verified: bool = True
) -> Any:
    number = await stack.service.register_number(
        org_id,
        RegisterPhoneNumberRequest(account_id=account.id, e164=e164, inbound_enabled=True),
    )
    if verified:
        number = await stack.service.set_number_verified(org_id, number.id, verified=True)
    return number


def _sign(secret: str, body: bytes) -> dict[str, str]:
    ts = str(int(time.time()))
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Telephony-Signature": f"sha256={digest}", "X-Telephony-Timestamp": ts}


_WEBHOOK_SECRET = "tel-webhook-secret"


def _ctx(body: dict[str, Any], *, secret: str = _WEBHOOK_SECRET) -> WebhookContext:
    raw = json.dumps(body).encode()
    return WebhookContext(method="POST", headers=_sign(secret, raw), query={}, body=raw)


async def test_account_and_number_lifecycle(telephony_stack: Any, make_organization: Any) -> None:
    org = await make_organization()
    account = await _account(telephony_stack, org.id)
    assert account.status is AccountStatus.ACTIVE
    assert account.public_view().receive_path.startswith("/api/v1/webhooks/telephony/fake/")

    number = await _number(telephony_stack, org.id, account, verified=False)
    assert not number.verified
    number = await telephony_stack.service.set_number_verified(org.id, number.id, verified=True)
    assert number.verified

    disabled = await telephony_stack.service.set_account_status(
        org.id, account.id, AccountStatus.DISABLED
    )
    assert disabled.status is AccountStatus.DISABLED


async def test_outbound_call_happy_path_emits_created_event(
    telephony_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(telephony_stack, org.id)
    number = await _number(telephony_stack, org.id, account)

    call = await telephony_stack.service.create_call(
        org.id,
        None,
        CreateCallRequest(
            provider_account_id=account.id,
            from_number_id=number.id,
            destination="+14155550199",
        ),
    )
    assert call.direction is CallDirection.OUTBOUND
    assert call.state is CallState.CREATED
    assert call.provider_call_id is not None
    assert call.from_address == "+14155550100"
    assert call.to_address == "+14155550199"
    assert len(telephony_stack.transport.requests) == 1

    async with telephony_stack.database.tenant_transaction(org.id) as tenant:
        events = (
            await tenant.session.execute(
                text(
                    "SELECT event_type FROM event_outbox "
                    "WHERE event_type = 'telephony.call.created'"
                )
            )
        ).all()
    assert len(events) == 1


async def test_outbound_call_requires_a_verified_owned_number(
    telephony_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(telephony_stack, org.id)
    unverified = await _number(telephony_stack, org.id, account, verified=False)
    with pytest.raises(TelephonyConfigInvalidError):
        await telephony_stack.service.create_call(
            org.id,
            None,
            CreateCallRequest(
                provider_account_id=account.id,
                from_number_id=unverified.id,
                destination="+14155550199",
            ),
        )


async def test_outbound_idempotency_replays_without_a_second_provider_call(
    telephony_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(telephony_stack, org.id)
    number = await _number(telephony_stack, org.id, account)
    request = CreateCallRequest(
        provider_account_id=account.id,
        from_number_id=number.id,
        destination="+14155550199",
        idempotency_key="tel-key-abc123",
    )
    first = await telephony_stack.service.create_call(org.id, None, request)
    replay = await telephony_stack.service.create_call(org.id, None, request)
    assert replay.id == first.id
    assert len(telephony_stack.transport.requests) == 1

    with pytest.raises(TelephonyIdempotencyConflictError):
        await telephony_stack.service.create_call(
            org.id,
            None,
            CreateCallRequest(
                provider_account_id=account.id,
                from_number_id=number.id,
                destination="+14155550188",
                idempotency_key="tel-key-abc123",
            ),
        )


async def test_outbound_idempotency_fingerprint_rejects_every_semantic_change(
    telephony_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account_a = await _account(telephony_stack, org.id)
    account_b = await _account(telephony_stack, org.id)
    number_a = await _number(telephony_stack, org.id, account_a, e164="+14155550101")
    number_b = await _number(telephony_stack, org.id, account_a, e164="+14155550102")
    number_on_b = await _number(telephony_stack, org.id, account_b, e164="+14155550103")

    key = "tel-fp-key-000111"
    original = await telephony_stack.service.create_call(
        org.id,
        None,
        CreateCallRequest(
            provider_account_id=account_a.id,
            from_number_id=number_a.id,
            destination="+14155550199",
            idempotency_key=key,
            metadata={"campaign": "spring"},
        ),
    )
    sends_after_original = len(telephony_stack.transport.requests)

    # 2. same key + different from_number_id -> conflict, never replays the wrong caller ID
    with pytest.raises(TelephonyIdempotencyConflictError):
        await telephony_stack.service.create_call(
            org.id,
            None,
            CreateCallRequest(
                provider_account_id=account_a.id,
                from_number_id=number_b.id,
                destination="+14155550199",
                idempotency_key=key,
                metadata={"campaign": "spring"},
            ),
        )

    # 4. same key + different provider_account_id -> conflict
    with pytest.raises(TelephonyIdempotencyConflictError):
        await telephony_stack.service.create_call(
            org.id,
            None,
            CreateCallRequest(
                provider_account_id=account_b.id,
                from_number_id=number_on_b.id,
                destination="+14155550199",
                idempotency_key=key,
                metadata={"campaign": "spring"},
            ),
        )

    # 5. same key + different semantic metadata -> conflict
    with pytest.raises(TelephonyIdempotencyConflictError):
        await telephony_stack.service.create_call(
            org.id,
            None,
            CreateCallRequest(
                provider_account_id=account_a.id,
                from_number_id=number_a.id,
                destination="+14155550199",
                idempotency_key=key,
                metadata={"campaign": "autumn"},
            ),
        )

    # an identical replay (only correlation_id differs — observational) still replays
    replay = await telephony_stack.service.create_call(
        org.id,
        None,
        CreateCallRequest(
            provider_account_id=account_a.id,
            from_number_id=number_a.id,
            destination="+14155550199",
            idempotency_key=key,
            correlation_id="a-different-trace-id",
            metadata={"campaign": "spring"},
        ),
    )
    assert replay.id == original.id
    # not one of the rejected variants placed a second provider call
    assert len(telephony_stack.transport.requests) == sends_after_original


async def test_stale_provider_sequence_does_not_advance_call_state(
    telephony_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(telephony_stack, org.id)
    number = await _number(telephony_stack, org.id, account)
    call = await telephony_stack.service.create_call(
        org.id,
        None,
        CreateCallRequest(
            provider_account_id=account.id, from_number_id=number.id, destination="+14155550199"
        ),
    )

    async def _event(name: str, sequence: int) -> None:
        await telephony_stack.inbound.receive(
            "fake",
            account.webhook_token,
            _ctx(
                {
                    "event_id": uuid4().hex,
                    "call_id": call.provider_call_id,
                    "event": name,
                    "sequence": sequence,
                }
            ),
        )

    await _event("ANSWERED", 5)
    # a BRIDGED the provider orders BEFORE the recorded ANSWERED -> ignored
    await _event("BRIDGED", 3)
    async with telephony_stack.database.tenant_transaction(org.id) as tenant:
        state = (
            await tenant.session.execute(
                text("SELECT state FROM telephony_calls WHERE id = :i"), {"i": call.id}
            )
        ).scalar_one()
    assert state == "ANSWERED"

    # a genuinely newer BRIDGED advances the call
    await _event("BRIDGED", 9)
    async with telephony_stack.database.tenant_transaction(org.id) as tenant:
        state = (
            await tenant.session.execute(
                text("SELECT state FROM telephony_calls WHERE id = :i"), {"i": call.id}
            )
        ).scalar_one()
    assert state == "BRIDGED"


async def test_hangup_is_terminal_safe_and_idempotent(
    telephony_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(telephony_stack, org.id)
    number = await _number(telephony_stack, org.id, account)
    call = await telephony_stack.service.create_call(
        org.id,
        None,
        CreateCallRequest(
            provider_account_id=account.id, from_number_id=number.id, destination="+14155550199"
        ),
    )
    hung = await telephony_stack.service.hangup_call(org.id, call.id, HangupCallRequest())
    assert hung.state is CallState.ENDING
    again = await telephony_stack.service.hangup_call(org.id, call.id, HangupCallRequest())
    assert again.state is CallState.ENDING  # idempotent, no rollback


async def test_dtmf_requires_an_answered_call(telephony_stack: Any, make_organization: Any) -> None:
    org = await make_organization()
    account = await _account(telephony_stack, org.id)
    number = await _number(telephony_stack, org.id, account)
    call = await telephony_stack.service.create_call(
        org.id,
        None,
        CreateCallRequest(
            provider_account_id=account.id, from_number_id=number.id, destination="+14155550199"
        ),
    )
    with pytest.raises(TelephonyInvalidStateError):
        await telephony_stack.service.send_dtmf(org.id, call.id, SendDtmfRequest(digits="123"))

    # advance to ANSWERED via a signed provider callback, then DTMF works
    body = {
        "event_id": uuid4().hex,
        "call_id": call.provider_call_id,
        "event": "ANSWERED",
        "timestamp": int(time.time()),
    }
    await telephony_stack.inbound.receive("fake", account.webhook_token, _ctx(body))
    result = await telephony_stack.service.send_dtmf(
        org.id, call.id, SendDtmfRequest(digits="12*#")
    )
    assert result.accepted


async def test_inbound_call_ingestion_and_lifecycle(
    telephony_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(telephony_stack, org.id)
    dialed = await _number(telephony_stack, org.id, account, e164="+14155550100")

    provider_call = f"pc-{uuid4().hex}"
    inbound_body = {
        "kind": "inbound",
        "event_id": uuid4().hex,
        "call_id": provider_call,
        "from": "+14155550142",
        "to": dialed.e164,
        "timestamp": int(time.time()),
    }
    acc = await telephony_stack.inbound.receive("fake", account.webhook_token, _ctx(inbound_body))
    assert acc.accepted and acc.processed == "created"

    async with telephony_stack.database.tenant_transaction(org.id) as tenant:
        row = (
            await tenant.session.execute(
                text("SELECT direction, state FROM telephony_calls WHERE provider_call_id = :p"),
                {"p": provider_call},
            )
        ).one()
    assert row.direction == "INBOUND" and row.state == "RINGING"

    # ANSWERED then COMPLETED
    for name in ("ANSWERED", "COMPLETED"):
        await telephony_stack.inbound.receive(
            "fake",
            account.webhook_token,
            _ctx(
                {
                    "event_id": uuid4().hex,
                    "call_id": provider_call,
                    "event": name,
                    "timestamp": int(time.time()),
                }
            ),
        )
    async with telephony_stack.database.tenant_transaction(org.id) as tenant:
        state = (
            await tenant.session.execute(
                text("SELECT state, disposition FROM telephony_calls WHERE provider_call_id = :p"),
                {"p": provider_call},
            )
        ).one()
    assert state.state == "COMPLETED" and state.disposition == "ANSWERED"


async def test_out_of_order_completed_then_ringing_keeps_completed(
    telephony_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(telephony_stack, org.id)
    dialed = await _number(telephony_stack, org.id, account, e164="+14155550100")
    provider_call = f"pc-{uuid4().hex}"

    await telephony_stack.inbound.receive(
        "fake",
        account.webhook_token,
        _ctx(
            {
                "kind": "inbound",
                "event_id": uuid4().hex,
                "call_id": provider_call,
                "from": "+14155550142",
                "to": dialed.e164,
            }
        ),
    )
    await telephony_stack.inbound.receive(
        "fake",
        account.webhook_token,
        _ctx({"event_id": uuid4().hex, "call_id": provider_call, "event": "COMPLETED"}),
    )
    # a delayed RINGING arrives AFTER COMPLETED
    result = await telephony_stack.inbound.receive(
        "fake",
        account.webhook_token,
        _ctx({"event_id": uuid4().hex, "call_id": provider_call, "event": "RINGING"}),
    )
    assert "ignored" in result.processed
    async with telephony_stack.database.tenant_transaction(org.id) as tenant:
        state = (
            await tenant.session.execute(
                text("SELECT state FROM telephony_calls WHERE provider_call_id = :p"),
                {"p": provider_call},
            )
        ).scalar_one()
    assert state == "COMPLETED"


async def test_duplicate_provider_event_is_processed_once(
    telephony_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(telephony_stack, org.id)
    dialed = await _number(telephony_stack, org.id, account, e164="+14155550100")
    provider_call = f"pc-{uuid4().hex}"
    await telephony_stack.inbound.receive(
        "fake",
        account.webhook_token,
        _ctx(
            {
                "kind": "inbound",
                "event_id": uuid4().hex,
                "call_id": provider_call,
                "from": "+14155550142",
                "to": dialed.e164,
            }
        ),
    )
    event_id = uuid4().hex
    body = {"event_id": event_id, "call_id": provider_call, "event": "ANSWERED"}
    first = await telephony_stack.inbound.receive("fake", account.webhook_token, _ctx(body))
    second = await telephony_stack.inbound.receive("fake", account.webhook_token, _ctx(body))
    assert "applied" in first.processed
    assert second.processed == "replayed"


async def test_unsigned_or_stale_callback_is_rejected(
    telephony_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(telephony_stack, org.id)
    body = json.dumps({"event_id": "x", "call_id": "y", "event": "RINGING"}).encode()

    unsigned = WebhookContext(method="POST", headers={}, query={}, body=body)
    with pytest.raises(TelephonyWebhookInvalidError):
        await telephony_stack.inbound.receive("fake", account.webhook_token, unsigned)

    stale_ts = str(int(time.time()) - 4000)
    digest = hmac.new(
        b"tel-webhook-secret", f"{stale_ts}.".encode() + body, hashlib.sha256
    ).hexdigest()
    stale = WebhookContext(
        method="POST",
        headers={"X-Telephony-Signature": f"sha256={digest}", "X-Telephony-Timestamp": stale_ts},
        query={},
        body=body,
    )
    from nexus_ai.telephony.errors import TelephonyWebhookReplayError

    with pytest.raises(TelephonyWebhookReplayError):
        await telephony_stack.inbound.receive("fake", account.webhook_token, stale)


async def test_get_call_unknown_is_not_found(telephony_stack: Any, make_organization: Any) -> None:
    org = await make_organization()
    with pytest.raises(TelephonyCallNotFoundError):
        await telephony_stack.service.get_call(org.id, uuid4())
