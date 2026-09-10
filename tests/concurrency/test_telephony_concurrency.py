"""Telephony concurrency guarantees, proven against real PostgreSQL (NXS-P11)."""

from __future__ import annotations

import asyncio
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
from nexus_ai.telephony.errors import TelephonyIdempotencyConflictError
from nexus_ai.telephony.providers.base import WebhookContext

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


async def _number(stack: Any, org_id: Any, account: Any, e164: str = "+14155550100") -> Any:
    n = await stack.service.register_number(
        org_id, RegisterPhoneNumberRequest(account_id=account.id, e164=e164)
    )
    return await stack.service.set_number_verified(org_id, n.id, verified=True)


def _ctx(body: dict[str, Any]) -> WebhookContext:
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    digest = hmac.new(b"tel-secret", f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    return WebhookContext(
        method="POST",
        headers={"X-Telephony-Signature": f"sha256={digest}", "X-Telephony-Timestamp": ts},
        query={},
        body=raw,
    )


async def _ready_inbound_call(stack: Any, org_id: Any, account: Any) -> str:
    dialed = await _number(stack, org_id, account, "+14155550100")
    provider_call = f"pc-{uuid4().hex}"
    await stack.inbound.receive(
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
    return provider_call


async def test_duplicate_provider_events_persist_exactly_one_transition(
    telephony_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(telephony_stack, org.id)
    provider_call = await _ready_inbound_call(telephony_stack, org.id, account)
    body = {"event_id": uuid4().hex, "call_id": provider_call, "event": "ANSWERED"}

    results = await asyncio.gather(
        *(
            telephony_stack.inbound.receive("fake", account.webhook_token, _ctx(body))
            for _ in range(8)
        ),
        return_exceptions=True,
    )
    ok = [r for r in results if not isinstance(r, Exception)]
    applied = [r for r in ok if "applied" in r.processed]
    replayed = [r for r in ok if r.processed == "replayed"]
    assert len(applied) == 1
    assert len(replayed) == 7

    async with telephony_stack.database.tenant_transaction(org.id) as tenant:
        count = (
            await tenant.session.execute(
                text("SELECT count(*) FROM telephony_call_events WHERE provider_event_id = :e"),
                {"e": body["event_id"]},
            )
        ).scalar_one()
    assert count == 1


async def test_answered_and_hangup_concurrent_yield_a_valid_terminal_state(
    telephony_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(telephony_stack, org.id)
    provider_call = await _ready_inbound_call(telephony_stack, org.id, account)
    async with telephony_stack.database.tenant_transaction(org.id) as tenant:
        call_id = (
            await tenant.session.execute(
                text("SELECT id FROM telephony_calls WHERE provider_call_id = :p"),
                {"p": provider_call},
            )
        ).scalar_one()

    answered = telephony_stack.inbound.receive(
        "fake",
        account.webhook_token,
        _ctx({"event_id": uuid4().hex, "call_id": provider_call, "event": "ANSWERED"}),
    )
    completed = telephony_stack.inbound.receive(
        "fake",
        account.webhook_token,
        _ctx({"event_id": uuid4().hex, "call_id": provider_call, "event": "COMPLETED"}),
    )
    await asyncio.gather(answered, completed, return_exceptions=True)

    async with telephony_stack.database.tenant_transaction(org.id) as tenant:
        state = (
            await tenant.session.execute(
                text("SELECT state FROM telephony_calls WHERE id = :i"), {"i": call_id}
            )
        ).scalar_one()
    # terminal wins regardless of interleaving; state is never rolled backward
    assert state == "COMPLETED"


async def test_two_concurrent_hangups_are_idempotent(
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
    results = await asyncio.gather(
        *(
            telephony_stack.service.hangup_call(org.id, call.id, HangupCallRequest())
            for _ in range(6)
        ),
        return_exceptions=True,
    )
    assert all(not isinstance(r, Exception) for r in results)
    assert {r.state.value for r in results} == {"ENDING"}


async def test_concurrent_identical_outbound_idempotent_create_is_one_call(
    telephony_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(telephony_stack, org.id)
    number = await _number(telephony_stack, org.id, account)

    async def _one(key: str = "tel-race-key-abc123") -> Any:
        return await telephony_stack.service.create_call(
            org.id,
            None,
            CreateCallRequest(
                provider_account_id=account.id,
                from_number_id=number.id,
                destination="+14155550199",
                idempotency_key=key,
            ),
        )

    results = await asyncio.gather(*(_one() for _ in range(8)), return_exceptions=True)
    ok = [r for r in results if not isinstance(r, Exception)]
    assert len(ok) == 8
    assert len({r.id for r in ok}) == 1
    assert not any(isinstance(r, TelephonyIdempotencyConflictError) for r in results)
    sends = [
        r
        for r in telephony_stack.transport.requests
        if r["method"] == "POST" and "calls" in r["url"]
    ]
    assert len(sends) == 1


async def test_concurrent_same_key_different_caller_id_is_a_deterministic_conflict(
    telephony_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(telephony_stack, org.id)
    number_a = await _number(telephony_stack, org.id, account, "+14155550100")
    number_b = await _number(telephony_stack, org.id, account, "+14155550200")
    key = "tel-key-caller-race-1"

    async def _one(from_number_id: Any) -> Any:
        return await telephony_stack.service.create_call(
            org.id,
            None,
            CreateCallRequest(
                provider_account_id=account.id,
                from_number_id=from_number_id,
                destination="+14155550199",
                idempotency_key=key,
            ),
        )

    calls = [number_a.id if i % 2 == 0 else number_b.id for i in range(8)]
    results = await asyncio.gather(*(_one(nid) for nid in calls), return_exceptions=True)

    ok = [r for r in results if not isinstance(r, Exception)]
    conflicts = [r for r in results if isinstance(r, TelephonyIdempotencyConflictError)]
    other = [
        r
        for r in results
        if isinstance(r, Exception) and not isinstance(r, TelephonyIdempotencyConflictError)
    ]
    assert not other
    # exactly one caller-ID wins the key; every request for the other caller ID conflicts
    assert ok, "one request must win the idempotency key"
    assert len({r.id for r in ok}) == 1
    winner_number = ok[0].from_number_id
    assert {winner_number} == {number_a.id} or {winner_number} == {number_b.id}
    assert len(conflicts) >= 1
    assert len(ok) + len(conflicts) == 8

    # exactly one row and exactly one provider send — the losing caller ID is never dialed
    async with telephony_stack.database.tenant_transaction(org.id) as tenant:
        rows = (
            await tenant.session.execute(
                text("SELECT count(*) FROM telephony_calls WHERE idempotency_key = :k"), {"k": key}
            )
        ).scalar_one()
    assert rows == 1
    sends = [
        r
        for r in telephony_stack.transport.requests
        if r["method"] == "POST" and "calls" in r["url"]
    ]
    assert len(sends) == 1


async def test_same_external_call_id_across_two_orgs_stays_isolated(
    telephony_stack: Any, make_organization: Any
) -> None:
    org_a = await make_organization()
    org_b = await make_organization()
    account_a = await _account(telephony_stack, org_a.id)
    account_b = await _account(telephony_stack, org_b.id)
    dialed_a = await _number(telephony_stack, org_a.id, account_a, "+14155550100")
    dialed_b = await _number(telephony_stack, org_b.id, account_b, "+14155550200")
    shared_call_id = f"pc-shared-{uuid4().hex}"

    for account, dialed in ((account_a, dialed_a), (account_b, dialed_b)):
        await telephony_stack.inbound.receive(
            "fake",
            account.webhook_token,
            _ctx(
                {
                    "kind": "inbound",
                    "event_id": uuid4().hex,
                    "call_id": shared_call_id,
                    "from": "+14155559999",
                    "to": dialed.e164,
                }
            ),
        )
    async with telephony_stack.database.tenant_transaction(org_a.id) as tenant:
        a_count = (
            await tenant.session.execute(
                text("SELECT count(*) FROM telephony_calls WHERE provider_call_id = :p"),
                {"p": shared_call_id},
            )
        ).scalar_one()
    assert a_count == 1  # RLS confines the count to org A; org B has its own row


async def test_terminal_completed_then_delayed_ringing_stays_completed_under_race(
    telephony_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(telephony_stack, org.id)
    provider_call = await _ready_inbound_call(telephony_stack, org.id, account)
    await telephony_stack.inbound.receive(
        "fake",
        account.webhook_token,
        _ctx({"event_id": uuid4().hex, "call_id": provider_call, "event": "COMPLETED"}),
    )
    await asyncio.gather(
        *(
            telephony_stack.inbound.receive(
                "fake",
                account.webhook_token,
                _ctx({"event_id": uuid4().hex, "call_id": provider_call, "event": "RINGING"}),
            )
            for _ in range(5)
        ),
        return_exceptions=True,
    )
    async with telephony_stack.database.tenant_transaction(org.id) as tenant:
        state = (
            await tenant.session.execute(
                text("SELECT state FROM telephony_calls WHERE provider_call_id = :p"),
                {"p": provider_call},
            )
        ).scalar_one()
    assert state == "COMPLETED"
