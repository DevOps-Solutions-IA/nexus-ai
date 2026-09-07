"""Provisioning domain values (NXS-ORG-001).

The onboarding contract is strongly validated and rejects unknown fields. The
idempotency fingerprint is the SHA-256 of the canonical JSON form of the payload
WITHOUT the idempotency key — two requests with the same key but different business
content collide deterministically. Caller-supplied ``organization_id`` is rejected by
``extra="forbid"``: Organization identity is server-generated, always.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from nexus_ai.core.errors import ValidationFailedError
from nexus_ai.domain.organizations.keys import validate_organization_key

_IDEMPOTENCY_KEY = re.compile(r"\A[A-Za-z0-9._:\-]{8,128}\Z")
_LOCALE = re.compile(r"\A[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*\Z")

_Trimmed = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ProvisioningRequestStatus(StrEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class OnboardingRequest(BaseModel):
    """Validated onboarding command. ``idempotency_key`` is REQUIRED: provisioning is a
    retry-safe, idempotent platform operation by contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    idempotency_key: str
    organization_key: str
    display_name: Annotated[_Trimmed, Field(max_length=200)]
    legal_name: Annotated[_Trimmed, Field(max_length=200)]
    country_code: Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True)]
    timezone: _Trimmed
    locale: str = Field(default="en-US", max_length=32)
    owner_user_id: UUID | None = None

    @field_validator("idempotency_key")
    @classmethod
    def _idempotency_key(cls, value: str) -> str:
        candidate = value.strip()
        if not _IDEMPOTENCY_KEY.match(candidate):
            raise ValidationFailedError(
                [{"field": "idempotency_key", "message": "must be 8-128 safe characters"}]
            )
        return candidate

    @field_validator("organization_key")
    @classmethod
    def _organization_key(cls, value: str) -> str:
        return validate_organization_key(value)

    @field_validator("display_name", "legal_name")
    @classmethod
    def _names(cls, value: str) -> str:
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
            raise ValidationFailedError(
                [{"field": "display_name", "message": "must not contain control chars"}]
            )
        return value

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
        import zoneinfo

        if value not in zoneinfo.available_timezones():
            raise ValidationFailedError(
                [{"field": "timezone", "message": "must be a valid IANA timezone"}]
            )
        return value

    @field_validator("locale")
    @classmethod
    def _locale(cls, value: str) -> str:
        if not _LOCALE.match(value):
            raise ValidationFailedError(
                [{"field": "locale", "message": "must be a BCP-47-style locale tag"}]
            )
        return value

    def canonical_payload(self) -> bytes:
        """Deterministic JSON of everything EXCEPT the idempotency key."""
        data = self.model_dump(mode="json", exclude={"idempotency_key"})
        return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_payload()).hexdigest()

    def idempotency_key_hash(self) -> str:
        return hashlib.sha256(self.idempotency_key.encode("utf-8")).hexdigest()


class ProvisioningResult(BaseModel):
    """Safe projection of a provisioning outcome. No secrets, no internal identifiers."""

    model_config = ConfigDict(frozen=True)

    organization_id: UUID
    organization_key: str
    organization_status: str
    provisioning_status: ProvisioningRequestStatus
    dashboard_revision: int


class ProvisioningStatusView(BaseModel):
    """The current Organization's provisioning workflow state."""

    model_config = ConfigDict(frozen=True)

    organization_id: UUID
    organization_key: str
    status: ProvisioningRequestStatus
    error_code: str | None
    requested_at: dt.datetime
    completed_at: dt.datetime | None


class OrganizationSettingsView(BaseModel):
    """Safe projection of P05-owned Organization settings."""

    model_config = ConfigDict(frozen=True)

    organization_id: UUID
    locale: str
    revision: int
    created_at: dt.datetime
    updated_at: dt.datetime
