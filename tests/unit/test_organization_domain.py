"""Organization domain values and lifecycle (NXS-ORG-002, NXS-ORG-003, sections 77, 79)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nexus_ai.core.errors import OrganizationInvalidStateError, ValidationFailedError
from nexus_ai.domain.organizations.entities import OrganizationDraft, OrganizationProfileUpdate
from nexus_ai.domain.organizations.keys import (
    RESERVED_KEYS,
    normalize_organization_key,
    validate_organization_key,
)
from nexus_ai.domain.organizations.status import (
    OrganizationStatus,
    assert_transition,
    can_transition,
    is_operational,
)

_INVALID = (ValidationFailedError, ValidationError)


class TestOrganizationKey:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Clinica San Jose", "clinica-san-jose"),
            ("  ACME__Health  ", "acme-health"),
            ("Café Münchën", "caf-mnchn"),
            ("multi---hyphen", "multi-hyphen"),
        ],
    )
    def test_normalisation_is_deterministic(self, raw: str, expected: str) -> None:
        assert normalize_organization_key(raw) == expected
        assert validate_organization_key(raw) == expected

    @pytest.mark.parametrize("reserved", sorted(RESERVED_KEYS)[:5])
    def test_reserved_keys_rejected(self, reserved: str) -> None:
        with pytest.raises(ValidationFailedError):
            validate_organization_key(reserved)

    @pytest.mark.parametrize("bad", ["", "  ", "ab", "x" * 60, "a b\tc", "!!!", "--"])
    def test_invalid_keys_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationFailedError):
            validate_organization_key(bad)

    def test_control_characters_rejected(self) -> None:
        with pytest.raises(ValidationFailedError):
            validate_organization_key("evil\x00key")


class TestOrganizationDraft:
    def _draft(self, **overrides: object) -> OrganizationDraft:
        base: dict[str, object] = {
            "organization_key": "acme-health",
            "display_name": "ACME Health",
            "legal_name": "ACME Health S.A.",
            "country_code": "cr",
            "timezone": "America/Costa_Rica",
        }
        base.update(overrides)
        return OrganizationDraft(**base)  # type: ignore[arg-type]

    def test_valid_draft_normalises(self) -> None:
        draft = self._draft(country_code="cr")
        assert draft.country_code == "CR"
        assert draft.organization_key == "acme-health"

    def test_unicode_names_allowed(self) -> None:
        assert self._draft(display_name="Clínica Ñandú 北京").display_name == "Clínica Ñandú 北京"

    @pytest.mark.parametrize("country", ["USA", "c", "12", "??"])
    def test_invalid_country_rejected(self, country: str) -> None:
        with pytest.raises(_INVALID):
            self._draft(country_code=country)

    @pytest.mark.parametrize("tz", ["Mars/Phobos", "", "GMT+3", "not-a-zone"])
    def test_invalid_timezone_rejected(self, tz: str) -> None:
        with pytest.raises(_INVALID):
            self._draft(timezone=tz)

    def test_control_char_in_name_rejected(self) -> None:
        with pytest.raises(ValidationFailedError):
            self._draft(display_name="bad\x01name")

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            self._draft(quota=100)

    def test_profile_update_changes_excludes_version(self) -> None:
        payload = OrganizationProfileUpdate(display_name="New Name", expected_version=3)
        assert payload.changes() == {"display_name": "New Name"}


class TestStatusMachine:
    @pytest.mark.parametrize(
        ("current", "target", "ok"),
        [
            (OrganizationStatus.PROVISIONING, OrganizationStatus.ACTIVE, True),
            (OrganizationStatus.PROVISIONING, OrganizationStatus.ARCHIVED, True),
            (OrganizationStatus.PROVISIONING, OrganizationStatus.SUSPENDED, False),
            (OrganizationStatus.ACTIVE, OrganizationStatus.SUSPENDED, True),
            (OrganizationStatus.ACTIVE, OrganizationStatus.ARCHIVED, True),
            (OrganizationStatus.ACTIVE, OrganizationStatus.PROVISIONING, False),
            (OrganizationStatus.SUSPENDED, OrganizationStatus.ACTIVE, True),
            (OrganizationStatus.SUSPENDED, OrganizationStatus.ARCHIVED, True),
            (OrganizationStatus.ARCHIVED, OrganizationStatus.ACTIVE, False),
            (OrganizationStatus.ARCHIVED, OrganizationStatus.SUSPENDED, False),
        ],
    )
    def test_transition_rules(
        self, current: OrganizationStatus, target: OrganizationStatus, ok: bool
    ) -> None:
        assert can_transition(current, target) is ok
        if ok:
            assert_transition(current, target)
        else:
            with pytest.raises(OrganizationInvalidStateError):
                assert_transition(current, target)

    def test_same_status_transition_rejected(self) -> None:
        with pytest.raises(OrganizationInvalidStateError):
            assert_transition(OrganizationStatus.ACTIVE, OrganizationStatus.ACTIVE)

    def test_only_active_is_operational(self) -> None:
        assert is_operational(OrganizationStatus.ACTIVE)
        for status in (
            OrganizationStatus.PROVISIONING,
            OrganizationStatus.SUSPENDED,
            OrganizationStatus.ARCHIVED,
        ):
            assert not is_operational(status)
