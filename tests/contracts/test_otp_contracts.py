"""OTP stable contracts — schemas, taxonomy, events, API surface (NXS-P10)."""

from __future__ import annotations

import pytest

from nexus_ai.core.errors import NxsError
from nexus_ai.events.registry import EVENT_REGISTRY
from nexus_ai.otp.entities import (
    IssueOtpRequest,
    IssueOtpResult,
    OtpChallengeView,
    OtpChannel,
    OtpStatus,
    VerifyOtpRequest,
    VerifyOtpResult,
)
from nexus_ai.otp.errors import OTP_ERRORS
from nexus_ai.otp.purposes import REGISTERED_PURPOSES

pytestmark = pytest.mark.anyio

_FORBIDDEN_ISSUE_FIELDS = {
    "provider",
    "url",
    "method",
    "headers",
    "template",
    "body",
    "organization_id",
    "code",
    "code_hash",
    "pepper",
}


def test_status_and_channel_enums_are_stable() -> None:
    assert [s.value for s in OtpStatus] == [
        "ACTIVE",
        "VERIFIED",
        "EXPIRED",
        "REVOKED",
        "LOCKED",
    ]
    assert [c.value for c in OtpChannel] == ["SMS", "EMAIL"]


def test_registered_purposes_are_allow_listed_and_stable() -> None:
    assert set(REGISTERED_PURPOSES) == {"GENERIC_VERIFICATION", "VERIFY_EMAIL", "VERIFY_PHONE"}


def test_issue_request_has_no_transport_or_secret_primitive() -> None:
    fields = set(IssueOtpRequest.model_fields)
    assert fields == {
        "purpose",
        "channel",
        "destination",
        "messaging_account_id",
        "default_country",
        "idempotency_key",
        "correlation_id",
    }
    assert not (fields & _FORBIDDEN_ISSUE_FIELDS)


def test_result_models_never_expose_a_code() -> None:
    for model in (IssueOtpResult, VerifyOtpResult, OtpChallengeView):
        assert "code" not in model.model_fields
        assert "code_hash" not in model.model_fields
        assert "destination" not in model.model_fields
    assert "masked_destination" in IssueOtpResult.model_fields
    assert "masked_destination" in OtpChallengeView.model_fields


def test_verify_request_is_strict_digits_only() -> None:
    from pydantic import ValidationError

    VerifyOtpRequest(code="123456")
    with pytest.raises(ValidationError):
        VerifyOtpRequest(code="abc123")
    with pytest.raises(ValidationError):
        VerifyOtpRequest(code="1", extra="x")  # type: ignore[call-arg]


def test_error_taxonomy_is_stable_unique_and_rfc9457() -> None:
    codes = [e.code for e in OTP_ERRORS]
    assert len(codes) == len(set(codes))
    assert set(codes) == {
        "NXS_OTP_CHALLENGE_NOT_FOUND",
        "NXS_OTP_INVALID",
        "NXS_OTP_EXPIRED",
        "NXS_OTP_ALREADY_USED",
        "NXS_OTP_LOCKED",
        "NXS_OTP_RATE_LIMITED",
        "NXS_OTP_RESEND_TOO_SOON",
        "NXS_OTP_DELIVERY_FAILED",
        "NXS_OTP_PURPOSE_INVALID",
        "NXS_OTP_CONFIG_INVALID",
        "NXS_OTP_IDEMPOTENCY_CONFLICT",
    }
    for error in OTP_ERRORS:
        assert issubclass(error, NxsError)
        assert error.code.startswith("NXS_OTP_")
        assert 400 <= error.status < 600


def test_event_payloads_registered_and_strict() -> None:
    from pydantic import ValidationError

    for event_type in (
        "otp.challenge.issued",
        "otp.challenge.delivery_failed",
        "otp.challenge.verified",
        "otp.challenge.failed_attempt",
        "otp.challenge.locked",
        "otp.challenge.expired",
        "otp.challenge.revoked",
        "otp.challenge.rate_limited",
    ):
        assert EVENT_REGISTRY.is_known_type(event_type)
        model = EVENT_REGISTRY.model_for(event_type, 1)
        with pytest.raises(ValidationError):
            model.model_validate({"unexpected": 1})


async def test_openapi_surface_is_governed_only(app_client) -> None:  # type: ignore[no-untyped-def]
    schema = (await app_client.get("/openapi.json")).json()
    paths = set(schema["paths"])
    assert "/api/v1/otp/challenges" in paths
    assert "/api/v1/otp/challenges/{challenge_id}" in paths
    assert "/api/v1/otp/challenges/{challenge_id}/verify" in paths
    assert "/api/v1/otp/challenges/{challenge_id}/resend" in paths
    for path in paths:
        assert not any(
            token in path for token in ("/otp/raw", "/otp/http", "/otp/send", "otp/provider")
        ), path

    issue = schema["paths"]["/api/v1/otp/challenges"]["post"]
    ref = issue["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    props = set(schema["components"]["schemas"][ref.split("/")[-1]]["properties"])
    assert props == {
        "purpose",
        "channel",
        "destination",
        "messaging_account_id",
        "default_country",
        "idempotency_key",
        "correlation_id",
    }
    result_ref = issue["responses"]["201"]["content"]["application/json"]["schema"]["$ref"]
    result_props = set(schema["components"]["schemas"][result_ref.split("/")[-1]]["properties"])
    assert "code" not in result_props
    assert "masked_destination" in result_props


def test_p09_messaging_still_works_independently_of_otp() -> None:
    # P09 must not become an OTP-specific subsystem: the messaging service has no import
    # of the OTP package.
    import inspect

    import nexus_ai.messaging.service as messaging_service

    source = inspect.getsource(messaging_service)
    assert "nexus_ai.otp" not in source
    assert "otp" not in {name.lower() for name in dir(messaging_service.MessagingService)}


def test_later_phases_remain_planned() -> None:
    import json
    from pathlib import Path

    registry = json.loads(Path(".nxs/phase-registry.json").read_text())
    phases = {p["id"]: p for p in registry["phases"]}
    for later in ("NXS-P12", "NXS-P13", "NXS-P14", "NXS-P15", "NXS-P16", "NXS-P17"):
        assert phases[later]["status"] == "PLANNED", later
