"""OTP resilience: NXS-P09 delivery failure modes, ambiguous timeouts, transaction
rollback and the cleanup seam (NXS-P10: NXS-OTP-001)."""

from __future__ import annotations

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
from nexus_ai.messaging.providers.base import TransportError
from nexus_ai.otp.entities import IssueOtpRequest, OtpChannel, OtpDeliveryStatus, OtpStatus
from nexus_ai.otp.errors import OtpDeliveryFailedError

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


def _request(account: Any, destination: str = "+14155550142") -> IssueOtpRequest:
    return IssueOtpRequest(
        purpose="GENERIC_VERIFICATION",
        channel=OtpChannel.SMS,
        destination=destination,
        messaging_account_id=account.id,
    )


async def _challenge_row(stack: Any, org_id: Any, challenge_id: Any) -> Any:
    async with stack.database.tenant_transaction(org_id) as tenant:
        return (
            await tenant.session.execute(
                text("SELECT status, delivery_message_id FROM otp_challenges WHERE id = :i"),
                {"i": challenge_id},
            )
        ).one()


async def test_definitive_delivery_failure_revokes_challenge(
    otp_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(otp_stack, org.id)
    otp_stack.transport.set_handler(lambda _e: (500, {"error": "boom"}))

    with pytest.raises(OtpDeliveryFailedError) as exc:
        await otp_stack.service.issue(org.id, _request(account))
    assert exc.value.code == "NXS_OTP_DELIVERY_FAILED"

    async with otp_stack.database.tenant_transaction(org.id) as tenant:
        row = (
            await tenant.session.execute(
                text("SELECT status FROM otp_challenges ORDER BY issued_at DESC LIMIT 1")
            )
        ).one()
    assert row.status == OtpStatus.REVOKED.value
    # a delivery_failed audit event is durably recorded
    async with otp_stack.database.tenant_transaction(org.id) as tenant:
        failed = (
            await tenant.session.execute(
                text(
                    "SELECT count(*) FROM event_outbox "
                    "WHERE event_type = 'otp.challenge.delivery_failed'"
                )
            )
        ).scalar_one()
    assert failed == 1


async def test_provider_429_is_a_delivery_failure_not_a_silent_retry(
    otp_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(otp_stack, org.id)
    otp_stack.transport.set_handler(lambda _e: (429, {"error": "slow down"}))
    with pytest.raises(OtpDeliveryFailedError):
        await otp_stack.service.issue(org.id, _request(account))
    # exactly one provider call — no blind resend
    assert len(otp_stack.transport.requests) == 1


async def test_ambiguous_timeout_leaves_challenge_active_and_does_not_resend(
    otp_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(otp_stack, org.id)

    def _timeout(_entry: dict[str, Any]) -> TransportError:
        return TransportError("provider timed out", timeout=True)

    otp_stack.transport.set_handler(_timeout)
    result = await otp_stack.service.issue(org.id, _request(account))
    assert result.delivery is OtpDeliveryStatus.UNCONFIRMED
    assert result.status is OtpStatus.ACTIVE

    row = await _challenge_row(otp_stack, org.id, result.challenge_id)
    assert row.status == OtpStatus.ACTIVE.value  # still usable if the code did arrive
    assert len(otp_stack.transport.requests) == 1  # never blindly resent


async def test_cleanup_seam_purges_terminal_challenges_only(
    otp_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(otp_stack, org.id)
    active = await otp_stack.service.issue(org.id, _request(account, "+14155550142"))
    code = re.search(r"\b(\d{6})\b", json.loads(otp_stack.transport.requests[-1]["body"])["text"])
    assert code is not None

    async with otp_stack.database.tenant_transaction(org.id) as tenant:
        await tenant.session.execute(text("UPDATE otp_challenges SET resend_after = now()"))
    verified = await otp_stack.service.issue(org.id, _request(account, "+14155550143"))
    vcode = re.search(r"\b(\d{6})\b", json.loads(otp_stack.transport.requests[-1]["body"])["text"])
    assert vcode is not None
    from nexus_ai.otp.entities import VerifyOtpRequest

    await otp_stack.service.verify(
        org.id, verified.challenge_id, VerifyOtpRequest(code=vcode.group(1))
    )
    # age the terminal row past retention
    async with otp_stack.database.tenant_transaction(org.id) as tenant:
        await tenant.session.execute(
            text("UPDATE otp_challenges SET updated_at = :t WHERE id = :i"),
            {"t": dt.datetime.now(dt.UTC) - dt.timedelta(days=40), "i": verified.challenge_id},
        )

    purged = await otp_stack.service.purge_expired(org.id, older_than_seconds=86_400)
    assert purged == 1
    remaining = await _challenge_row(otp_stack, org.id, active.challenge_id)
    assert remaining.status == OtpStatus.ACTIVE.value  # the active challenge is untouched


async def test_delivery_crash_after_challenge_row_is_recoverable(
    otp_stack: Any, make_organization: Any
) -> None:
    """If delivery raises a generic error the challenge does not stay usable — it is
    revoked, so a stale unusable code is never left ACTIVE."""
    org = await make_organization()
    account = await _account(otp_stack, org.id)

    def _explode(_entry: dict[str, Any]) -> TransportError:
        return TransportError("connection reset")

    otp_stack.transport.set_handler(_explode)
    with pytest.raises(OtpDeliveryFailedError):
        await otp_stack.service.issue(org.id, _request(account))
    async with otp_stack.database.tenant_transaction(org.id) as tenant:
        statuses = [
            r.status
            for r in (await tenant.session.execute(text("SELECT status FROM otp_challenges"))).all()
        ]
    assert statuses == [OtpStatus.REVOKED.value]
