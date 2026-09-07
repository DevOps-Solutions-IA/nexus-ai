"""Identity normalization and password policy (NXS-AUTH-001, NXS-AUTH-003)."""

from __future__ import annotations

import pytest

from nexus_ai.core.errors import ValidationFailedError
from nexus_ai.domain.auth.identity import normalize_email, validate_password


class TestNormalizeEmail:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("User@Example.com", "user@example.com"),
            ("  user@example.com  ", "user@example.com"),
            ("USER@EXAMPLE.COM", "user@example.com"),
            ("first.last+tag@sub.domain.example.com", "first.last+tag@sub.domain.example.com"),
        ],
    )
    def test_normalizes_to_canonical_form(self, raw: str, expected: str) -> None:
        assert normalize_email(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "not-an-email",
            "missing@tld",
            "two@@signs.com",
            "@nouser.com",
            "user@",
            "user@-leadinghyphen.com",
            "user@trailing-.com",
            "a" * 65 + "@example.com",  # local part too long
            "user@" + "a" * 64 + ".com",  # domain label too long
            "user@example.com" + "x" * 300,  # total too long
            "user@exa mple.com",  # embedded space
            "us\x00er@example.com",  # control char
        ],
    )
    def test_rejects_invalid_addresses(self, raw: str) -> None:
        with pytest.raises(ValidationFailedError):
            normalize_email(raw)

    def test_non_ascii_local_parts_rejected_identically_for_all_forms(self) -> None:
        # Conservative ASCII-only address policy: composed and decomposed spellings of
        # the same non-ASCII local part are BOTH rejected — never stored differently.
        composed = "useré@example.com"  # é precomposed
        decomposed = "useré@example.com"  # e + combining acute
        with pytest.raises(ValidationFailedError):
            normalize_email(composed)
        with pytest.raises(ValidationFailedError):
            normalize_email(decomposed)

    def test_error_carries_field_name(self) -> None:
        with pytest.raises(ValidationFailedError) as excinfo:
            normalize_email("not-an-email")
        assert excinfo.value.extensions["errors"][0]["field"] == "email"


class TestValidatePassword:
    @pytest.mark.parametrize(
        "value", ["correct-horse-battery", "a" * 128, "pässwörd-ünïcode-1", "0" * 12]
    )
    def test_accepts_bounded_passwords(self, value: str) -> None:
        assert validate_password(value, min_length=12, max_length=128) == value

    @pytest.mark.parametrize("value", ["", "short", "a" * 11, "a" * 129])
    def test_rejects_out_of_bounds_lengths(self, value: str) -> None:
        with pytest.raises(ValidationFailedError):
            validate_password(value, min_length=12, max_length=128)

    @pytest.mark.parametrize(
        "value", ["with\ttab-password", "with\nnewline-password", "with\x07bell-password"]
    )
    def test_rejects_control_characters(self, value: str) -> None:
        with pytest.raises(ValidationFailedError):
            validate_password(value, min_length=12, max_length=128)

    def test_never_normalizes_the_password(self) -> None:
        # Decomposed input must pass through byte-for-byte: the verifier depends on it.
        decomposed = "éééééééééééé"
        assert validate_password(decomposed, min_length=12, max_length=128) == decomposed

    def test_pathological_megabyte_input_rejected_in_bounded_time(self) -> None:
        with pytest.raises(ValidationFailedError):
            validate_password("a" * 1_000_000, min_length=12, max_length=128)
