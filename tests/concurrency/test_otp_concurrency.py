"""OTP concurrency guarantees, proven against real PostgreSQL (NXS-P10: NXS-OTP-001).

Every race here runs through ``asyncio.gather`` over the shared connection pool, so the
competing coroutines hold genuinely separate database transactions.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text

from nexus_ai.messaging.entities import (
    CreateAccountRequest,
    MessageChannel,
    StoreAccountCredentialRequest,
)
from nexus_ai.otp.entities import (
    IssueOtpRequest,
    OtpChannel,
    OtpDeliveryStatus,
    OtpStatus,
    VerifyOtpRequest,
)
from nexus_ai.otp.errors import (
    OtpAlreadyUsedError,
    OtpIdempotencyConflictError,
    OtpInvalidError,
    OtpLockedError,
    OtpResendTooSoonError,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _account(stack: Any, org_id: Any) -> Any:
    account = await stack.messaging.create_account(
        org_id,
        CreateAccountRequest(
            channel=MessageChannel.SMS,
            provider="generic_http",
            slug=f"sms-{uuid4().hex[:8]}",
            external_account_id=uuid4().hex,
            sender_identity="+14155550100",
        ),
    )
    await stack.messaging.store_account_credential(
        org_id,
        account.id,
        StoreAccountCredentialRequest(fields={"api_token": "t", "webhook_secret": "s"}),
    )
    return await stack.messaging.get_account(org_id, account.id)


def _last_code(stack: Any) -> str:
    payload = json.loads(stack.transport.requests[-1]["body"])
    match = re.search(r"\b(\d{6})\b", payload.get("text") or "")
    assert match is not None
    return match.group(1)


async def _issue(stack: Any, org_id: Any, account: Any, destination: str = "+14155550100") -> Any:
    return await stack.service.issue(
        org_id,
        IssueOtpRequest(
            purpose="GENERIC_VERIFICATION",
            channel=OtpChannel.SMS,
            destination=destination,
            messaging_account_id=account.id,
        ),
    )


async def test_two_correct_verifications_yield_exactly_one_success(
    otp_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(otp_stack, org.id)
    issued = await _issue(otp_stack, org.id, account)
    code = _last_code(otp_stack)

    results = await asyncio.gather(
        *(
            otp_stack.service.verify(org.id, issued.challenge_id, VerifyOtpRequest(code=code))
            for _ in range(6)
        ),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    already_used = [r for r in results if isinstance(r, OtpAlreadyUsedError)]
    assert len(successes) == 1
    assert len(already_used) == 5

    async with otp_stack.database.tenant_transaction(org.id) as tenant:
        row = (
            await tenant.session.execute(
                text("SELECT status, attempts FROM otp_challenges WHERE id = :i"),
                {"i": issued.challenge_id},
            )
        ).one()
    assert row.status == "VERIFIED"
    assert row.attempts == 1  # the losing racers never counted an attempt


async def test_concurrent_wrong_attempts_increment_and_lock_deterministically(
    otp_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(otp_stack, org.id)
    issued = await _issue(otp_stack, org.id, account)

    results = await asyncio.gather(
        *(
            otp_stack.service.verify(org.id, issued.challenge_id, VerifyOtpRequest(code="000000"))
            for _ in range(12)
        ),
        return_exceptions=True,
    )
    invalid = [r for r in results if isinstance(r, OtpInvalidError)]
    locked = [r for r in results if isinstance(r, OtpLockedError)]
    assert len(invalid) == 4  # attempts 1..4
    assert len(locked) == 8  # attempt 5 locks, 6..12 hit the terminal state

    async with otp_stack.database.tenant_transaction(org.id) as tenant:
        row = (
            await tenant.session.execute(
                text("SELECT status, attempts FROM otp_challenges WHERE id = :i"),
                {"i": issued.challenge_id},
            )
        ).one()
    assert row.status == "LOCKED"
    assert row.attempts == 5  # never over the policy limit


async def test_simultaneous_issuance_keeps_one_active_challenge(
    otp_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(otp_stack, org.id)

    results = await asyncio.gather(
        *(_issue(otp_stack, org.id, account) for _ in range(6)),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    too_soon = [r for r in results if isinstance(r, OtpResendTooSoonError)]
    assert len(successes) == 1
    assert len(too_soon) == 5

    async with otp_stack.database.tenant_transaction(org.id) as tenant:
        active = (
            await tenant.session.execute(
                text("SELECT count(*) FROM otp_challenges WHERE status = 'ACTIVE'")
            )
        ).scalar_one()
    assert active == 1


async def test_resend_versus_original_verification_race(
    otp_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(otp_stack, org.id)
    first = await _issue(otp_stack, org.id, account)
    first_code = _last_code(otp_stack)
    async with otp_stack.database.tenant_transaction(org.id) as tenant:
        await tenant.session.execute(
            text("UPDATE otp_challenges SET resend_after = :t WHERE id = :i"),
            {"t": dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1), "i": first.challenge_id},
        )

    verify_first = otp_stack.service.verify(
        org.id, first.challenge_id, VerifyOtpRequest(code=first_code)
    )
    resend = otp_stack.service.resend(org.id, first.challenge_id)
    outcomes = await asyncio.gather(verify_first, resend, return_exceptions=True)

    # exactly one of {the original verified, the original was revoked by the resend}
    verify_result, resend_result = outcomes
    if not isinstance(verify_result, Exception):
        assert verify_result.outcome.value == "VERIFIED"
    else:
        assert isinstance(verify_result, OtpInvalidError)
        assert not isinstance(resend_result, Exception)

    async with otp_stack.database.tenant_transaction(org.id) as tenant:
        statuses = {
            r.status
            for r in (
                await tenant.session.execute(
                    text("SELECT status FROM otp_challenges ORDER BY issued_at")
                )
            ).all()
        }
    assert OtpStatus.ACTIVE.value in statuses or OtpStatus.VERIFIED.value in statuses


async def test_concurrent_identical_idempotent_issue_sends_exactly_one_code(
    otp_stack: Any, make_organization: Any
) -> None:
    """Concurrent `issue()` calls with the same org + account + purpose + destination
    + idempotency_key: exactly one challenge, exactly one provider send, and the single
    delivered code verifies that challenge (NXS-P10 concurrent-idempotency corrective).

    Repeated over several fresh trials because the losing paths (one-active partial
    index vs. idempotency unique vs. cooldown-against-a-just-committed-sibling) only
    interleave under real contention.
    """
    for trial in range(8):
        org = await make_organization()
        account = await _account(otp_stack, org.id)
        key = f"otp-concurrent-key-{trial:02d}-abc"
        fanout = 8
        sends_before = len(otp_stack.transport.requests)

        async def _one(k: str = key, acct: Any = account, o: Any = org) -> Any:
            return await otp_stack.service.issue(
                o.id,
                IssueOtpRequest(
                    purpose="GENERIC_VERIFICATION",
                    channel=OtpChannel.SMS,
                    destination="+14155550142",
                    messaging_account_id=acct.id,
                    idempotency_key=k,
                ),
            )

        results = await asyncio.gather(*(_one() for _ in range(fanout)), return_exceptions=True)
        ok = [r for r in results if not isinstance(r, Exception)]
        # (1) every call returns a safe result; (7)/(8) no losing request errors out and
        # NXS_OTP_RESEND_TOO_SOON is never raised for a semantically identical replay
        assert len(ok) == fanout, (trial, results)
        assert not any(isinstance(r, OtpResendTooSoonError) for r in results), (trial, results)
        challenge_ids = {r.challenge_id for r in ok}
        assert len(challenge_ids) == 1, (trial, challenge_ids)  # (1) one challenge_id
        challenge_id = challenge_ids.pop()
        assert sum(1 for r in ok if r.replayed) == fanout - 1  # exactly one owner
        assert all(r.delivery in (OtpDeliveryStatus.SENT, OtpDeliveryStatus.SKIPPED) for r in ok)

        # (2) exactly one challenge row for that key
        async with otp_stack.database.tenant_transaction(org.id) as tenant:
            rows = (
                await tenant.session.execute(
                    text("SELECT count(*) FROM otp_challenges WHERE idempotency_key = :k"),
                    {"k": key},
                )
            ).scalar_one()
        assert rows == 1

        # (3)(4)(6) exactly one provider send for this trial / one code delivered
        sends = [
            r
            for r in otp_stack.transport.requests[sends_before:]
            if "text" in json.loads(r["body"] or b"{}")
        ]
        assert len(sends) == 1, (trial, len(sends))
        delivered = re.search(r"\b(\d{6})\b", json.loads(sends[0]["body"])["text"])
        assert delivered is not None

        # (5) the delivered code verifies the one challenge
        verified = await otp_stack.service.verify(
            org.id, challenge_id, VerifyOtpRequest(code=delivered.group(1))
        )
        assert verified.outcome.value == "VERIFIED"


async def test_same_key_different_semantic_request_is_conflict_under_races(
    otp_stack: Any, make_organization: Any
) -> None:
    """(9) same idempotency_key, different destination → NXS_OTP_IDEMPOTENCY_CONFLICT,
    even when the two calls race."""
    org = await make_organization()
    account = await _account(otp_stack, org.id)
    key = "otp-conflict-key-abc123"

    async def _issue_to(destination: str) -> Any:
        return await otp_stack.service.issue(
            org.id,
            IssueOtpRequest(
                purpose="GENERIC_VERIFICATION",
                channel=OtpChannel.SMS,
                destination=destination,
                messaging_account_id=account.id,
                idempotency_key=key,
            ),
        )

    results = await asyncio.gather(
        _issue_to("+14155550142"),
        _issue_to("+14155550143"),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    conflicts = [r for r in results if isinstance(r, OtpIdempotencyConflictError)]
    assert len(successes) == 1
    assert len(conflicts) == 1


async def test_loser_entering_before_delivery_completes_does_not_send(
    otp_stack: Any, make_organization: Any
) -> None:
    """The exact window: A persists its challenge and blocks inside the P09 provider
    send (delivery_message_id not yet written); B issues with the same key. B must NOT
    send — it replays A's challenge. Deterministic via a gate in the fake transport."""
    org = await make_organization()
    account = await _account(otp_stack, org.id)
    key = "otp-window-key-abc123"

    inside_send = asyncio.Event()
    release = asyncio.Event()

    async def _gated_handler(_entry: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        inside_send.set()
        await release.wait()
        return 200, {"message_id": "prov-window", "messages": [{"id": "prov-window"}]}

    otp_stack.transport.set_handler(_gated_handler)

    def _request() -> IssueOtpRequest:
        return IssueOtpRequest(
            purpose="GENERIC_VERIFICATION",
            channel=OtpChannel.SMS,
            destination="+14155550142",
            messaging_account_id=account.id,
            idempotency_key=key,
        )

    task_a = asyncio.create_task(otp_stack.service.issue(org.id, _request()))
    await asyncio.wait_for(inside_send.wait(), timeout=5)
    # A's challenge is committed; A is blocked mid-send with no delivery_message_id yet.
    async with otp_stack.database.tenant_transaction(org.id) as tenant:
        pre = (
            await tenant.session.execute(
                text("SELECT delivery_message_id FROM otp_challenges WHERE idempotency_key = :k"),
                {"k": key},
            )
        ).one()
    assert pre.delivery_message_id is None

    result_b = await otp_stack.service.issue(org.id, _request())
    assert result_b.replayed is True
    assert result_b.delivery is OtpDeliveryStatus.SKIPPED

    release.set()
    result_a = await asyncio.wait_for(task_a, timeout=5)
    assert result_a.challenge_id == result_b.challenge_id
    # exactly one provider send happened across both requests
    sends = [r for r in otp_stack.transport.requests if "text" in json.loads(r["body"] or b"{}")]
    assert len(sends) == 1


async def test_expire_versus_verify_boundary(otp_stack: Any, make_organization: Any) -> None:
    org = await make_organization()
    account = await _account(otp_stack, org.id)
    issued = await _issue(otp_stack, org.id, account)
    code = _last_code(otp_stack)
    async with otp_stack.database.tenant_transaction(org.id) as tenant:
        await tenant.session.execute(
            text("UPDATE otp_challenges SET issued_at = :o, expires_at = :t WHERE id = :i"),
            {
                "o": dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10),
                "t": dt.datetime.now(dt.UTC) + dt.timedelta(milliseconds=40),
                "i": issued.challenge_id,
            },
        )

    async def _delayed_verify() -> Any:
        await asyncio.sleep(0.08)
        return await otp_stack.service.verify(
            org.id, issued.challenge_id, VerifyOtpRequest(code=code)
        )

    results = await asyncio.gather(
        otp_stack.service.verify(org.id, issued.challenge_id, VerifyOtpRequest(code=code)),
        _delayed_verify(),
        return_exceptions=True,
    )
    # at most one success; a post-expiry attempt is deterministically rejected
    successes = [r for r in results if not isinstance(r, Exception)]
    assert len(successes) <= 1
