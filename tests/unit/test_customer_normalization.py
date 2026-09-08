"""Customer identity normalization and validation (NXS-CUSTOMER-001, ADR-0052)."""

from __future__ import annotations

import pytest

from nexus_ai.core.errors import IdentityNormalizationError, UnsupportedIdentityTypeError
from nexus_ai.domain.customers.entities import IdentityType
from nexus_ai.domain.customers.normalization import (
    normalize_external_id,
    normalize_identity_value,
    normalize_phone,
)


class TestEmailNormalization:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("User@Example.com", "user@example.com"),
            ("  user@example.com  ", "user@example.com"),
            ("first.last+tag@sub.example.com", "first.last+tag@sub.example.com"),
        ],
    )
    def test_normalizes(self, raw: str, expected: str) -> None:
        assert normalize_identity_value(IdentityType.EMAIL, raw) == expected

    @pytest.mark.parametrize(
        "raw", ["not-an-email", "user@", "@host", "user@exa mple.com", "a\x00b@example.com"]
    )
    def test_rejects_malformed(self, raw: str) -> None:
        with pytest.raises(IdentityNormalizationError):
            normalize_identity_value(IdentityType.EMAIL, raw)

    def test_normalization_collision_unifies_representations(self) -> None:
        # The canonical value is what uniqueness operates on.
        first = normalize_identity_value(IdentityType.EMAIL, "JOHN@Example.COM")
        second = normalize_identity_value(IdentityType.EMAIL, "john@example.com")
        assert first == second


class TestPhoneNormalization:
    def test_e164_with_plus_accepted(self) -> None:
        assert normalize_phone("+506 8888-7777") == "+50688887777"

    def test_leading_zeros_normalized(self) -> None:
        assert normalize_phone("0050688887777") == "+50688887777"

    def test_national_number_requires_country_context(self) -> None:
        with pytest.raises(IdentityNormalizationError, match="refusing to guess"):
            normalize_phone("88887777")

    def test_national_number_with_country(self) -> None:
        assert normalize_phone("8888 7777", default_country="506") == "+50688887777"

    @pytest.mark.parametrize("raw", ["", "+123", "+" + "1" * 16, "abc", "+506\x00x"])
    def test_rejects_impossible_numbers(self, raw: str) -> None:
        with pytest.raises(IdentityNormalizationError):
            normalize_phone(raw)

    def test_normalization_collision(self) -> None:
        first = normalize_phone("+506-8888-7777")
        second = normalize_phone("00506 8888 7777")
        assert first == second


class TestExternalIdNormalization:
    def test_trims_and_bounds(self) -> None:
        assert normalize_external_id("  acme-cust-001  ") == "acme-cust-001"

    @pytest.mark.parametrize("raw", ["", "a" * 161, "bad\x01id"])
    def test_rejects_unsafe(self, raw: str) -> None:
        with pytest.raises(IdentityNormalizationError):
            normalize_external_id(raw)

    def test_never_globally_trusted(self) -> None:
        # External ids are namespaced opaque values: resolution always includes
        # organization scope (proven in the integration/security suites).
        assert normalize_external_id("anything-at-all") == "anything-at-all"


class TestUnsupportedTypes:
    def test_unsupported_identity_type_fails_closed(self) -> None:
        with pytest.raises(UnsupportedIdentityTypeError):
            normalize_identity_value("SSN", "123-45-6789")  # type: ignore[arg-type]
