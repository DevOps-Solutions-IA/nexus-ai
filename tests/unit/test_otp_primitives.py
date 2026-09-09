"""OTP primitives: code generation, the keyed verifier, masking, templates, purposes,
and typed settings (NXS-P10)."""

from __future__ import annotations

import re
from uuid import uuid4

import pytest
from pydantic import ValidationError

from nexus_ai.core.config import OtpSettings, Settings
from nexus_ai.otp.codes import (
    canonical_context,
    destination_fingerprint,
    generate_code,
    hash_code,
    mask_destination,
    verify_code,
)
from nexus_ai.otp.entities import CURRENT_HASH_VERSION, IssueOtpRequest, OtpChannel
from nexus_ai.otp.errors import OtpPurposeInvalidError
from nexus_ai.otp.purposes import REGISTERED_PURPOSES, resolve_purpose
from nexus_ai.otp.templates import render_message

_PEPPER = b"x" * 48


def test_generate_code_is_digits_of_requested_length_and_high_entropy() -> None:
    codes = {generate_code(6) for _ in range(500)}
    assert all(re.fullmatch(r"\d{6}", code) for code in codes)
    # 500 CSPRNG draws over a 10**6 space essentially never collide en masse.
    assert len(codes) > 490
    assert re.fullmatch(r"\d{8}", generate_code(8))


@pytest.mark.parametrize("length", [0, 1, 5, 11, 20])
def test_generate_code_rejects_unsafe_lengths(length: int) -> None:
    with pytest.raises(ValueError):
        generate_code(length)


def test_hash_is_keyed_constant_time_and_binds_context() -> None:
    org, challenge = uuid4(), uuid4()
    ctx = canonical_context(
        organization_id=org,
        challenge_id=challenge,
        purpose="GENERIC_VERIFICATION",
        channel="SMS",
        destination="+14155550100",
        hash_version=CURRENT_HASH_VERSION,
    )
    digest = hash_code(_PEPPER, ctx, "123456")
    assert verify_code(_PEPPER, ctx, "123456", digest)
    assert not verify_code(_PEPPER, ctx, "654321", digest)
    # a different pepper never verifies
    assert not verify_code(b"y" * 48, ctx, "123456", digest)
    # a different context (purpose / destination / challenge / org) never verifies
    other = canonical_context(
        organization_id=org,
        challenge_id=challenge,
        purpose="VERIFY_PHONE",
        channel="SMS",
        destination="+14155550100",
        hash_version=CURRENT_HASH_VERSION,
    )
    assert not verify_code(_PEPPER, other, "123456", digest)


def test_destination_fingerprint_is_stable_and_channel_scoped() -> None:
    a = destination_fingerprint(_PEPPER, "SMS", "+14155550100")
    assert a == destination_fingerprint(_PEPPER, "SMS", "+14155550100")
    assert a != destination_fingerprint(_PEPPER, "EMAIL", "+14155550100")
    assert a != destination_fingerprint(b"z" * 48, "SMS", "+14155550100")


@pytest.mark.parametrize(
    ("channel", "destination", "expected_contains", "expected_absent"),
    [
        ("EMAIL", "alice@example.com", "@", "alice"),
        ("SMS", "+14155550100", "0100", "4155"),
    ],
)
def test_mask_destination_keeps_context_hides_secret(
    channel: str, destination: str, expected_contains: str, expected_absent: str
) -> None:
    masked = mask_destination(channel, destination)
    assert expected_contains in masked
    assert expected_absent not in masked
    assert destination != masked


def test_templates_are_deterministic_minimal_and_carry_the_code_once() -> None:
    sms = render_message(OtpChannel.SMS, "123456", 300)
    assert sms.subject is None
    assert sms.text.count("123456") == 1
    assert "5 minutes" in sms.text
    email = render_message(OtpChannel.EMAIL, "123456", 300)
    assert email.subject == "Nexus verification code"
    assert render_message(OtpChannel.SMS, "123456", 60).text.count("1 minute") == 1


def test_purpose_registry_is_allow_listed_and_channel_scoped() -> None:
    assert "GENERIC_VERIFICATION" in REGISTERED_PURPOSES
    resolve_purpose("GENERIC_VERIFICATION", OtpChannel.SMS)
    resolve_purpose("VERIFY_EMAIL", OtpChannel.EMAIL)
    with pytest.raises(OtpPurposeInvalidError):
        resolve_purpose("TOTALLY_MADE_UP", OtpChannel.SMS)
    with pytest.raises(OtpPurposeInvalidError):
        resolve_purpose("VERIFY_EMAIL", OtpChannel.SMS)


def test_issue_request_is_strict_and_bounded() -> None:
    ok = IssueOtpRequest(
        purpose="GENERIC_VERIFICATION",
        channel=OtpChannel.SMS,
        destination="+14155550100",
        messaging_account_id=uuid4(),
    )
    assert ok.idempotency_key is None
    with pytest.raises(ValidationError):
        IssueOtpRequest(
            purpose="lower_case_bad",
            channel=OtpChannel.SMS,
            destination="+14155550100",
            messaging_account_id=uuid4(),
        )
    with pytest.raises(ValidationError):
        IssueOtpRequest(
            purpose="GENERIC_VERIFICATION",
            channel=OtpChannel.SMS,
            destination="+14155550100",
            messaging_account_id=uuid4(),
            provider="evil",  # type: ignore[call-arg]
        )


def test_settings_pepper_bounds_and_hardened_requirement() -> None:
    with pytest.raises(ValidationError):
        OtpSettings(pepper="short")
    with pytest.raises(ValidationError):
        OtpSettings(pepper="a" * 40, allow_ephemeral_pepper=True)
    # a hardened environment fails fast without a pepper
    with pytest.raises(ValidationError, match="NXS_OTP__PEPPER"):
        Settings(
            environment="production",
            auth={"signing_key": "0" * 64, "issuer": "i", "audience": "a"},
        )
