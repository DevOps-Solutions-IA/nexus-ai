"""OTP issuance + verification against the real database, event outbox and the NXS-P09
messaging stack with a fake provider transport (NXS-P10: NXS-OTP-001)."""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text

from nexus_ai.messaging.entities import AccountStatus, CreateAccountRequest, MessageChannel
from nexus_ai.otp.entities import (
    IssueOtpRequest,
    OtpChannel,
    OtpDeliveryStatus,
    OtpStatus,
    VerifyOtpRequest,
)
from nexus_ai.otp.errors import (
    OtpAlreadyUsedError,
    OtpChallengeNotFoundError,
    OtpConfigInvalidError,
    OtpExpiredError,
    OtpIdempotencyConflictError,
    OtpInvalidError,
    OtpLockedError,
    OtpRateLimitedError,
    OtpResendTooSoonError,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

_SECRET = {"api_token": "prov-token", "webhook_secret": "prov-webhook-secret"}


async def _account(
    stack: Any,
    org_id: Any,
    channel: MessageChannel = MessageChannel.SMS,
    *,
    sender: str = "+14155550100",
) -> Any:
    account = await stack.messaging.create_account(
        org_id,
        CreateAccountRequest(
            channel=channel,
            provider="generic_http",
            slug=f"{channel.value.lower()}-{uuid4().hex[:8]}",
            external_account_id=uuid4().hex,
            sender_identity=sender,
        ),
    )
    from nexus_ai.messaging.entities import StoreAccountCredentialRequest

    await stack.messaging.store_account_credential(
        org_id, account.id, StoreAccountCredentialRequest(fields=_SECRET)
    )
    return await stack.messaging.get_account(org_id, account.id)


def _last_code(stack: Any) -> str:
    body = stack.transport.requests[-1]["body"]
    payload = json.loads(body)
    text_field = payload.get("text") or payload.get("html") or ""
    match = re.search(r"\b(\d{6})\b", text_field)
    assert match is not None, text_field
    return match.group(1)


async def _issue(stack: Any, org_id: Any, account: Any, **overrides: Any) -> Any:
    request = IssueOtpRequest(
        purpose=overrides.pop("purpose", "GENERIC_VERIFICATION"),
        channel=overrides.pop("channel", OtpChannel.SMS),
        destination=overrides.pop("destination", "+14155550100"),
        messaging_account_id=account.id,
        **overrides,
    )
    return await stack.service.issue(org_id, request)


async def test_issue_delivers_and_returns_no_code(otp_stack: Any, make_organization: Any) -> None:
    org = await make_organization()
    account = await _account(otp_stack, org.id)

    result = await _issue(otp_stack, org.id, account)

    assert result.status is OtpStatus.ACTIVE
    assert result.delivery is OtpDeliveryStatus.SENT
    assert result.masked_destination.endswith("0100") and "4155" not in result.masked_destination
    dumped = json.dumps(result.model_dump(mode="json"))
    code = _last_code(otp_stack)
    assert code not in dumped and "code_hash" not in dumped and "pepper" not in dumped
    # a message row exists and is linked
    async with otp_stack.database.tenant_transaction(org.id) as tenant:
        row = (
            await tenant.session.execute(
                text("SELECT delivery_message_id, code_hash FROM otp_challenges WHERE id = :i"),
                {"i": result.challenge_id},
            )
        ).one()
    assert row.delivery_message_id is not None
    assert code not in row.code_hash


async def test_verify_happy_path_is_single_use(otp_stack: Any, make_organization: Any) -> None:
    org = await make_organization()
    account = await _account(otp_stack, org.id)
    result = await _issue(otp_stack, org.id, account)
    code = _last_code(otp_stack)

    verified = await otp_stack.service.verify(
        org.id, result.challenge_id, VerifyOtpRequest(code=code)
    )
    assert verified.outcome.value == "VERIFIED"

    with pytest.raises(OtpAlreadyUsedError):
        await otp_stack.service.verify(org.id, result.challenge_id, VerifyOtpRequest(code=code))


async def test_wrong_code_then_lockout(otp_stack: Any, make_organization: Any) -> None:
    org = await make_organization()
    account = await _account(otp_stack, org.id)
    result = await _issue(otp_stack, org.id, account)
    code = _last_code(otp_stack)

    for _ in range(4):
        with pytest.raises(OtpInvalidError):
            await otp_stack.service.verify(
                org.id, result.challenge_id, VerifyOtpRequest(code="000000")
            )
    # fifth wrong attempt locks
    with pytest.raises(OtpLockedError):
        await otp_stack.service.verify(org.id, result.challenge_id, VerifyOtpRequest(code="000000"))
    # the correct code no longer works after lock
    with pytest.raises(OtpLockedError):
        await otp_stack.service.verify(org.id, result.challenge_id, VerifyOtpRequest(code=code))


async def test_expiry_is_deterministic_rejection(otp_stack: Any, make_organization: Any) -> None:
    org = await make_organization()
    account = await _account(otp_stack, org.id)
    result = await _issue(otp_stack, org.id, account)
    code = _last_code(otp_stack)

    async with otp_stack.database.tenant_transaction(org.id) as tenant:
        await tenant.session.execute(
            text("UPDATE otp_challenges SET issued_at = :old, expires_at = :t WHERE id = :i"),
            {
                "old": dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10),
                "t": dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1),
                "i": result.challenge_id,
            },
        )
    with pytest.raises(OtpExpiredError):
        await otp_stack.service.verify(org.id, result.challenge_id, VerifyOtpRequest(code=code))
    view = await otp_stack.service.get_challenge(org.id, result.challenge_id)
    assert view.status is OtpStatus.EXPIRED


async def test_reissue_revokes_prior_active_and_enforces_cooldown(
    otp_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(otp_stack, org.id)
    first = await _issue(otp_stack, org.id, account)
    first_code = _last_code(otp_stack)

    # a resend inside the cooldown window is refused
    with pytest.raises(OtpResendTooSoonError):
        await _issue(otp_stack, org.id, account)

    # push the cooldown into the past, then reissue
    async with otp_stack.database.tenant_transaction(org.id) as tenant:
        await tenant.session.execute(
            text("UPDATE otp_challenges SET resend_after = :t WHERE id = :i"),
            {"t": dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1), "i": first.challenge_id},
        )
    second = await _issue(otp_stack, org.id, account)
    assert second.challenge_id != first.challenge_id

    # the first code is now dead; the second verifies
    with pytest.raises(OtpInvalidError):
        await otp_stack.service.verify(
            org.id, first.challenge_id, VerifyOtpRequest(code=first_code)
        )
    verified = await otp_stack.service.verify(
        org.id, second.challenge_id, VerifyOtpRequest(code=_last_code(otp_stack))
    )
    assert verified.outcome.value == "VERIFIED"


async def test_issuance_throttle_per_subject_and_purpose(
    otp_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(otp_stack, org.id)
    # relax the cooldown so only the window ceiling bites
    for _ in range(otp_stack.settings.otp.max_issues_per_window):
        result = await _issue(otp_stack, org.id, account)
        async with otp_stack.database.tenant_transaction(org.id) as tenant:
            await tenant.session.execute(
                text("UPDATE otp_challenges SET resend_after = :t WHERE id = :i"),
                {"t": dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1), "i": result.challenge_id},
            )
    with pytest.raises(OtpRateLimitedError):
        await _issue(otp_stack, org.id, account)


async def test_idempotency_replay_returns_same_challenge_without_resending(
    otp_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(otp_stack, org.id)
    first = await _issue(otp_stack, org.id, account, idempotency_key="otp-key-abc123")
    sent_count = len(otp_stack.transport.requests)

    replay = await _issue(otp_stack, org.id, account, idempotency_key="otp-key-abc123")
    assert replay.challenge_id == first.challenge_id
    assert replay.replayed is True
    assert replay.delivery is OtpDeliveryStatus.SKIPPED
    assert len(otp_stack.transport.requests) == sent_count

    with pytest.raises(OtpIdempotencyConflictError):
        await _issue(
            otp_stack,
            org.id,
            account,
            destination="+14155550199",
            idempotency_key="otp-key-abc123",
        )


async def test_resend_endpoint_reissues_for_same_subject(
    otp_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(otp_stack, org.id)
    first = await _issue(otp_stack, org.id, account)
    async with otp_stack.database.tenant_transaction(org.id) as tenant:
        await tenant.session.execute(
            text("UPDATE otp_challenges SET resend_after = :t WHERE id = :i"),
            {"t": dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1), "i": first.challenge_id},
        )
    resent = await otp_stack.service.resend(org.id, first.challenge_id)
    assert resent.challenge_id != first.challenge_id
    assert resent.delivery is OtpDeliveryStatus.SENT


async def test_unknown_or_disabled_account_is_config_invalid(
    otp_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    with pytest.raises(OtpConfigInvalidError):
        await otp_stack.service.issue(
            org.id,
            IssueOtpRequest(
                purpose="GENERIC_VERIFICATION",
                channel=OtpChannel.SMS,
                destination="+14155550100",
                messaging_account_id=uuid4(),
            ),
        )
    account = await _account(otp_stack, org.id)
    await otp_stack.messaging.set_account_status(org.id, account.id, AccountStatus.DISABLED)
    with pytest.raises(OtpConfigInvalidError):
        await _issue(otp_stack, org.id, account)


async def test_channel_account_mismatch_is_rejected(otp_stack: Any, make_organization: Any) -> None:
    org = await make_organization()
    email_account = await _account(
        otp_stack, org.id, MessageChannel.EMAIL, sender="otp@example.com"
    )
    with pytest.raises(OtpConfigInvalidError):
        await _issue(otp_stack, org.id, email_account, channel=OtpChannel.SMS)


async def test_get_challenge_unknown_is_not_found(otp_stack: Any, make_organization: Any) -> None:
    org = await make_organization()
    with pytest.raises(OtpChallengeNotFoundError):
        await otp_stack.service.get_challenge(org.id, uuid4())
