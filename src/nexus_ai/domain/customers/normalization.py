"""Deterministic customer identity normalization (NXS-CUSTOMER-001).

Normalized values — and ONLY normalized values — are what uniqueness and resolution
operate on. What is normalized and what is NOT is documented explicitly (ADR-0052):

* EMAIL — trim, lowercase, NFC, conservative ASCII validation (the SAME rules as the
  platform user email identity: one normalization vocabulary for the whole product).
* PHONE — canonical E.164 when country context is available. A leading ``+`` with
  8-15 digits is accepted; digits without a country code REQUIRE an explicit
  ``default_country`` (never guessed silently — ambiguity fails closed); whitespace,
  dashes, parentheses and a leading ``00`` are normalized away.
* EXTERNAL_ID — trim, NFC, bounded, control-character-free; a namespaced opaque
  identifier that is NEVER globally trusted (resolution always includes
  organization scope).
"""

from __future__ import annotations

import re
import unicodedata

from nexus_ai.core.errors import (
    IdentityNormalizationError,
    UnsupportedIdentityTypeError,
    ValidationFailedError,
)
from nexus_ai.domain.auth.identity import normalize_email as _normalize_platform_email
from nexus_ai.domain.customers.entities import IdentityType

_PHONE_DIGITS = re.compile(r"[^\d]")
_PLUS_DIGITS = re.compile(r"\A\+\d{8,15}\Z")
_COUNTRY_CODE = re.compile(r"\A[1-9]\d{0,2}\Z")
_EXTERNAL_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MAX_EXTERNAL_LENGTH = 160


def normalize_identity_value(
    identity_type: IdentityType, raw: str, *, default_country: str | None = None
) -> str:
    """Deterministic normalization for the given identity type. Fails closed on any
    malformed or ambiguous input — never guesses, never stores raw values."""
    if identity_type is IdentityType.EMAIL:
        try:
            return _normalize_platform_email(raw)
        except ValidationFailedError as exc:
            raise IdentityNormalizationError("the email identity is malformed", cause=exc) from exc
    if identity_type is IdentityType.PHONE:
        return normalize_phone(raw, default_country=default_country)
    if identity_type is IdentityType.EXTERNAL_ID:
        return normalize_external_id(raw)
    raise UnsupportedIdentityTypeError(
        "this identity type is not supported", extensions={"identity_type": str(identity_type)}
    )


def normalize_phone(raw: str, *, default_country: str | None = None) -> str:
    candidate = unicodedata.normalize("NFKC", raw).strip()
    candidate = f"+{candidate[2:]}" if candidate.startswith("00") else candidate
    if not candidate:
        raise IdentityNormalizationError("phone identity must not be empty")
    if _EXTERNAL_CONTROL.search(candidate):
        raise IdentityNormalizationError("phone identity must not contain control characters")
    if candidate.startswith("+"):
        digits = _PHONE_DIGITS.sub("", candidate[1:])
        if not _PLUS_DIGITS.match(f"+{digits}"):
            raise IdentityNormalizationError("phone identity must be 8-15 digits in E.164 form")
        return f"+{digits}"
    # No explicit country context: ambiguous by definition — fail closed.
    digits = _PHONE_DIGITS.sub("", candidate)
    if not digits:
        raise IdentityNormalizationError("phone identity must not be empty")
    if default_country is None or not _COUNTRY_CODE.match(default_country):
        raise IdentityNormalizationError(
            "a phone identity without a leading '+' requires an explicit country code; "
            "refusing to guess silently"
        )
    e164 = f"+{default_country}{digits}"
    if not _PLUS_DIGITS.match(e164):
        raise IdentityNormalizationError("phone identity must be 8-15 digits in E.164 form")
    return e164


def normalize_external_id(raw: str) -> str:
    candidate = unicodedata.normalize("NFC", raw).strip()
    if not candidate or len(candidate) > MAX_EXTERNAL_LENGTH:
        raise IdentityNormalizationError("external identity must be 1-160 characters")
    if _EXTERNAL_CONTROL.search(candidate):
        raise IdentityNormalizationError("external identity must not contain control characters")
    return candidate
