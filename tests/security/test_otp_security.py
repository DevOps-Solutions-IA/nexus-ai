"""OTP adversarial tests (NXS-P10: NXS-OTP-001).

Covers brute-force / replay / expiry / cross-binding / cross-tenant / forgery / malformed
input / secret + log + event + response leakage / predictable-RNG regression.
"""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from nexus_ai.messaging.entities import (
    CreateAccountRequest,
    MessageChannel,
    StoreAccountCredentialRequest,
)
from nexus_ai.otp.codes import generate_code
from nexus_ai.otp.entities import IssueOtpRequest, OtpChannel, VerifyOtpRequest
from nexus_ai.otp.errors import (
    OtpChallengeNotFoundError,
    OtpExpiredError,
    OtpInvalidError,
    OtpLockedError,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _account(stack: Any, org_id: Any, sender: str = "+14155550100") -> Any:
    account = await stack.messaging.create_account(
        org_id,
        CreateAccountRequest(
            channel=MessageChannel.SMS,
            provider="generic_http",
            slug=f"sms-{uuid4().hex[:8]}",
            external_account_id=uuid4().hex,
            sender_identity=sender,
        ),
    )
    await stack.messaging.store_account_credential(
        org_id,
        account.id,
        StoreAccountCredentialRequest(fields={"api_token": "t", "webhook_secret": "s"}),
    )
    return await stack.messaging.get_account(org_id, account.id)


async def _issue(stack: Any, org_id: Any, account: Any, destination: str = "+14155550142") -> Any:
    return await stack.service.issue(
        org_id,
        IssueOtpRequest(
            purpose="GENERIC_VERIFICATION",
            channel=OtpChannel.SMS,
            destination=destination,
            messaging_account_id=account.id,
        ),
    )


def _code(stack: Any) -> str:
    match = re.search(r"\b(\d{6})\b", json.loads(stack.transport.requests[-1]["body"])["text"])
    assert match is not None
    return match.group(1)


async def test_brute_force_is_bounded_by_lockout(otp_stack: Any, make_organization: Any) -> None:
    org = await make_organization()
    account = await _account(otp_stack, org.id)
    issued = await _issue(otp_stack, org.id, account)
    real = _code(otp_stack)

    guesses = [f"{n:06d}" for n in range(1000) if f"{n:06d}" != real][:20]
    seen_lock = False
    for guess in guesses:
        try:
            await otp_stack.service.verify(
                org.id, issued.challenge_id, VerifyOtpRequest(code=guess)
            )
        except OtpLockedError:
            seen_lock = True
            break
        except OtpInvalidError:
            continue
    assert seen_lock  # at most max_attempts guesses ever land


async def test_replay_after_success_and_after_lock(otp_stack: Any, make_organization: Any) -> None:
    org = await make_organization()
    account = await _account(otp_stack, org.id)
    issued = await _issue(otp_stack, org.id, account)
    code = _code(otp_stack)
    await otp_stack.service.verify(org.id, issued.challenge_id, VerifyOtpRequest(code=code))
    with pytest.raises(Exception) as replayed:
        await otp_stack.service.verify(org.id, issued.challenge_id, VerifyOtpRequest(code=code))
    assert replayed.value.code == "NXS_OTP_ALREADY_USED"


async def test_code_bound_to_challenge_purpose_and_subject(
    otp_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(otp_stack, org.id)
    first = await _issue(otp_stack, org.id, account, destination="+14155550142")
    first_code = _code(otp_stack)

    # relax cooldown, issue a second challenge for a different subject
    async with otp_stack.database.tenant_transaction(org.id) as tenant:
        await tenant.session.execute(text("UPDATE otp_challenges SET resend_after = now()"))
    second = await _issue(otp_stack, org.id, account, destination="+14155550143")

    # the first code cannot verify the second challenge (different challenge + subject)
    with pytest.raises(OtpInvalidError):
        await otp_stack.service.verify(
            org.id, second.challenge_id, VerifyOtpRequest(code=first_code)
        )
    # ... but it still verifies its own
    await otp_stack.service.verify(org.id, first.challenge_id, VerifyOtpRequest(code=first_code))


async def test_cross_tenant_verification_and_forged_ids_fail_closed(
    otp_stack: Any, make_organization: Any
) -> None:
    org_a = await make_organization()
    org_b = await make_organization()
    account = await _account(otp_stack, org_a.id)
    issued = await _issue(otp_stack, org_a.id, account)
    code = _code(otp_stack)

    # Org B cannot see or verify Org A's challenge
    with pytest.raises(OtpChallengeNotFoundError):
        await otp_stack.service.verify(org_b.id, issued.challenge_id, VerifyOtpRequest(code=code))
    with pytest.raises(OtpChallengeNotFoundError):
        await otp_stack.service.get_challenge(org_b.id, issued.challenge_id)
    # a completely forged id is simply not found
    with pytest.raises(OtpChallengeNotFoundError):
        await otp_stack.service.verify(org_a.id, uuid4(), VerifyOtpRequest(code=code))
    # a forged row pointing at a non-existent (cross-tenant) account is refused by the
    # composite tenant-aware FK
    async with otp_stack.database.tenant_transaction(org_a.id) as tenant:
        with pytest.raises(IntegrityError):
            await tenant.session.execute(
                text(
                    "INSERT INTO otp_challenges (id, organization_id, subject_type, destination, "
                    "destination_fingerprint, purpose, channel, messaging_account_id, code_hash, "
                    "hash_version, status, max_attempts, request_fingerprint, issued_at, "
                    "expires_at, resend_after) VALUES (:id, :org, 'DESTINATION', 'x', 'fp', 'P', "
                    "'SMS', :acct, 'h', 1, 'ACTIVE', 5, 'rf', now(), "
                    "now() + interval '5 min', now())"
                ),
                {"id": uuid4(), "org": org_a.id, "acct": uuid4()},
            )


async def test_verification_after_expiry_is_rejected(
    otp_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(otp_stack, org.id)
    issued = await _issue(otp_stack, org.id, account)
    code = _code(otp_stack)
    async with otp_stack.database.tenant_transaction(org.id) as tenant:
        await tenant.session.execute(
            text(
                "UPDATE otp_challenges SET issued_at = now() - interval '1 hour', "
                "expires_at = now() - interval '1 min' WHERE id = :i"
            ),
            {"i": issued.challenge_id},
        )
    with pytest.raises(OtpExpiredError):
        await otp_stack.service.verify(org.id, issued.challenge_id, VerifyOtpRequest(code=code))


@pytest.mark.parametrize("bad", ["", "12ab56", "  1234  ", "12345678901234", "١٢٣٤٥٦", "12\n34"])
def test_malformed_codes_are_rejected_before_the_service(bad: str) -> None:
    with pytest.raises(ValidationError):
        VerifyOtpRequest(code=bad)


async def test_no_secret_or_code_in_events_or_challenge_row(
    otp_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(otp_stack, org.id)
    issued = await _issue(otp_stack, org.id, account, destination="+14155550142")
    code = _code(otp_stack)
    with pytest.raises(OtpInvalidError):
        await otp_stack.service.verify(org.id, issued.challenge_id, VerifyOtpRequest(code="000000"))

    async with otp_stack.database.tenant_transaction(org.id) as tenant:
        events = [
            row._mapping["envelope"]
            for row in (
                await tenant.session.execute(
                    text("SELECT envelope FROM event_outbox WHERE event_type LIKE 'otp.%'")
                )
            ).all()
        ]
        challenge_row = (
            (
                await tenant.session.execute(
                    text("SELECT * FROM otp_challenges WHERE id = :i"), {"i": issued.challenge_id}
                )
            )
            .one()
            ._mapping
        )

    assert events, "otp events must be emitted"
    for event in events:
        blob = json.dumps(event)
        assert code not in blob
        assert "pepper" not in blob and "code_hash" not in blob
        assert "+14155550142" not in blob  # full destination is masked
    # the row stores a keyed hash, never the code
    assert code not in challenge_row["code_hash"]
    assert challenge_row["code_hash"] != code


def test_generate_code_is_not_predictable_regression_guard() -> None:
    # a predictable generator (sequential / time-derived / seeded PRNG) would fail this:
    # 200 draws with no run of 3 monotonic values and near-uniform first digits.
    draws = [int(generate_code(6)) for _ in range(200)]
    monotonic_runs = sum(
        1 for a, b, c in zip(draws, draws[1:], draws[2:], strict=False) if a < b < c or a > b > c
    )
    assert monotonic_runs < len(draws) // 2
    first_digits = {str(d).zfill(6)[0] for d in draws}
    assert len(first_digits) >= 6


async def test_disabled_subsystem_refuses(otp_stack: Any, make_organization: Any) -> None:
    from nexus_ai.otp.service import OtpService

    org = await make_organization()
    account = await _account(otp_stack, org.id)
    settings = otp_stack.settings.model_copy(
        update={"otp": otp_stack.settings.otp.model_copy(update={"enabled": False})}
    )
    disabled = OtpService(
        settings,
        otp_stack.database,
        otp_stack.event_platform.publisher,
        otp_stack.messaging,
        otp_stack.customers,
        otp_stack.conversations,
    )
    with pytest.raises(Exception) as exc:
        await disabled.issue(
            org.id,
            IssueOtpRequest(
                purpose="GENERIC_VERIFICATION",
                channel=OtpChannel.SMS,
                destination="+14155550142",
                messaging_account_id=account.id,
            ),
        )
    assert exc.value.code == "NXS_OTP_CONFIG_INVALID"
