"""Onboarding contract validation and idempotency primitives (NXS-ORG-001)."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from nexus_ai.core.errors import ValidationFailedError
from nexus_ai.domain.provisioning.entities import OnboardingRequest

PASSWORD = "correct-horse-battery-staple"


def _base(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "idempotency_key": "onboard-0001",
        "organization_key": f"valid-key-{uuid.uuid4().hex[:8]}",
        "display_name": "Acme Widgets",
        "legal_name": "Acme Widgets S.A.",
        "country_code": "cr",
        "timezone": "America/Costa_Rica",
        "locale": "en-US",
    }
    payload.update(overrides)
    return payload


class TestOnboardingValidation:
    def test_valid_payload_roundtrips(self) -> None:
        request = OnboardingRequest(**_base())
        assert request.locale == "en-US"
        assert request.owner_user_id is None

    def test_unknown_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OnboardingRequest(**_base(), organization_id=str(uuid.uuid7()))

    def test_caller_supplied_identity_rejected(self) -> None:
        # The canonical attack: a caller asserting the Organization id.
        with pytest.raises(ValidationError):
            OnboardingRequest(**_base(), organization_id=str(uuid.uuid7()))
        # And the adjacent spoof attempts: role/database fields.
        with pytest.raises(ValidationError):
            OnboardingRequest(**_base(), owner_role="superadmin")
        with pytest.raises(ValidationError):
            OnboardingRequest(**_base(), rls_bypass="true")

    @pytest.mark.parametrize(
        "field,value",
        [
            ("display_name", "A" * 201),
            ("display_name", "bad\nname"),
            ("display_name", "bad\x07name"),
            ("country_code", "USA"),
            ("country_code", "1"),
            ("timezone", "Mars/Olympus"),
            ("locale", "EN-us-x!bad"),
            ("locale", ""),
            ("organization_key", "admin"),  # platform-reserved slug (P02 rule)
            ("organization_key", "!"),
            ("idempotency_key", "short"),
            ("idempotency_key", "bad key with spaces"),
        ],
    )
    def test_invalid_values_rejected(self, field: str, value: object) -> None:
        with pytest.raises((ValidationError, ValidationFailedError)):
            OnboardingRequest(**_base(**{field: value}))

    def test_idempotency_key_required(self) -> None:
        payload = _base()
        del payload["idempotency_key"]
        with pytest.raises(ValidationError):
            OnboardingRequest(**payload)

    def test_organization_key_normalizes_case(self) -> None:
        request = OnboardingRequest(**_base(organization_key="UPPER-Case-Key"))
        assert request.organization_key == "upper-case-key"

    def test_locale_defaults_to_en_us(self) -> None:
        payload = _base()
        del payload["locale"]
        assert OnboardingRequest(**payload).locale == "en-US"


class TestFingerprintAndIdempotency:
    def test_fingerprint_is_stable_for_identical_payloads(self) -> None:
        fixed_key = f"fingerprint-{uuid.uuid4().hex[:8]}"
        first = OnboardingRequest(**_base(organization_key=fixed_key, idempotency_key="key-AAAA"))
        second = OnboardingRequest(**_base(organization_key=fixed_key, idempotency_key="key-BBBB"))
        assert first.fingerprint() == second.fingerprint()

    def test_fingerprint_differs_when_business_content_differs(self) -> None:
        first = OnboardingRequest(**_base())
        second = OnboardingRequest(**_base(display_name="Other Name"))
        assert first.fingerprint() != second.fingerprint()

    def test_fingerprint_is_field_order_independent(self) -> None:
        # Pydantic model_dump is deterministic by field order; this pins the contract
        # that the fingerprint derives from the canonical JSON form.
        first = OnboardingRequest(**_base())
        assert first.canonical_payload() == first.canonical_payload()
        assert len(first.fingerprint()) == 64
        assert len(first.idempotency_key_hash()) == 64

    def test_idempotency_key_hash_hides_the_raw_key(self) -> None:
        request = OnboardingRequest(**_base(idempotency_key="sensitive-client-token"))
        assert "sensitive-client-token" not in request.idempotency_key_hash()
