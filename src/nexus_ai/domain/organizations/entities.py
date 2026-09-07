"""Organization domain values: validated inputs and safe representations (NXS-ORG-002).

ORM rows never cross the service boundary. ``Organization`` is the internal domain view;
``OrganizationView`` is the deliberately-safe public projection. ``tax_identifier`` is
internal-only and is excluded from the public view, ``repr`` and logs.
"""

from __future__ import annotations

import datetime as dt
import zoneinfo
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from nexus_ai.core.errors import ValidationFailedError
from nexus_ai.domain.organizations.keys import validate_organization_key
from nexus_ai.domain.organizations.status import OrganizationStatus

_Trimmed = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
_AVAILABLE_TIMEZONES = zoneinfo.available_timezones()


def _no_control_chars(value: str, field: str) -> str:
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValidationFailedError([{"field": field, "message": "must not contain control chars"}])
    return value


class OrganizationDraft(BaseModel):
    """Validated input to create the core Organization record (used by tests and future P05)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    organization_key: str
    display_name: Annotated[_Trimmed, Field(max_length=200)]
    legal_name: Annotated[_Trimmed, Field(max_length=200)]
    country_code: Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True)]
    timezone: _Trimmed
    industry_code: (
        Annotated[str, StringConstraints(strip_whitespace=True), Field(max_length=32)] | None
    ) = None
    tax_identifier: (
        Annotated[str, StringConstraints(strip_whitespace=True), Field(max_length=64)] | None
    ) = None

    @field_validator("organization_key")
    @classmethod
    def _key(cls, value: str) -> str:
        return validate_organization_key(value)

    @field_validator("display_name", "legal_name")
    @classmethod
    def _names(cls, value: str) -> str:
        return _no_control_chars(value, "display_name")

    @field_validator("country_code")
    @classmethod
    def _country(cls, value: str) -> str:
        if len(value) != 2 or not value.isalpha():
            raise ValidationFailedError(
                [{"field": "country_code", "message": "must be an ISO 3166-1 alpha-2 code"}]
            )
        return value

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str) -> str:
        if value not in _AVAILABLE_TIMEZONES:
            raise ValidationFailedError(
                [{"field": "timezone", "message": "must be a valid IANA timezone"}]
            )
        return value


class OrganizationProfileUpdate(BaseModel):
    """Partial, P02-appropriate profile changes for the current Organization."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    display_name: Annotated[_Trimmed, Field(max_length=200)] | None = None
    legal_name: Annotated[_Trimmed, Field(max_length=200)] | None = None
    timezone: _Trimmed | None = None
    industry_code: (
        Annotated[str, StringConstraints(strip_whitespace=True), Field(max_length=32)] | None
    ) = None
    expected_version: int = Field(ge=1)

    @field_validator("display_name", "legal_name")
    @classmethod
    def _names(cls, value: str | None) -> str | None:
        return None if value is None else _no_control_chars(value, "display_name")

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str | None) -> str | None:
        if value is not None and value not in _AVAILABLE_TIMEZONES:
            raise ValidationFailedError(
                [{"field": "timezone", "message": "must be a valid IANA timezone"}]
            )
        return value

    def changes(self) -> dict[str, str]:
        data = self.model_dump(exclude_none=True)
        data.pop("expected_version", None)
        return data


class Organization(BaseModel):
    """Internal domain view of an Organization (includes tax_identifier)."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_key: str
    display_name: str
    legal_name: str
    country_code: str
    timezone: str
    industry_code: str | None
    tax_identifier: str | None = Field(default=None, repr=False)
    status: OrganizationStatus
    version: int
    created_at: dt.datetime
    updated_at: dt.datetime
    activated_at: dt.datetime | None
    suspended_at: dt.datetime | None
    archived_at: dt.datetime | None

    def public_view(self) -> OrganizationView:
        return OrganizationView(
            id=self.id,
            organization_key=self.organization_key,
            display_name=self.display_name,
            country_code=self.country_code,
            timezone=self.timezone,
            industry_code=self.industry_code,
            status=self.status,
            version=self.version,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class OrganizationView(BaseModel):
    """Safe public projection: no legal_name, no tax_identifier, no lifecycle timestamps."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_key: str
    display_name: str
    country_code: str
    timezone: str
    industry_code: str | None
    status: OrganizationStatus
    version: int
    created_at: dt.datetime
    updated_at: dt.datetime
