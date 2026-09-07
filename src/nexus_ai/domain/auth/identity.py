"""Identity-plane validation primitives (NXS-AUTH-001, NXS-AUTH-003).

Email identities are normalized deterministically (trim, lowercase, NFC) before any
comparison, storage or lookup, so ``User@Example.com`` and ``user@example.com`` are the
same identity and uniqueness constraints are meaningful. Passwords are validated for
bounds and control characters but are NEVER normalized — normalization would change the
byte sequence the verifier sees and could break future provider migrations; the policy
is simply "as-typed, bounded, control-character free".
"""

from __future__ import annotations

import re
import unicodedata

from nexus_ai.core.errors import ValidationFailedError

_LOCAL_PART = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+$")
_DOMAIN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
MAX_LOCAL_LENGTH = 64
MAX_DOMAIN_LENGTH = 255
MAX_TOTAL_LENGTH = 254


def normalize_email(raw: str) -> str:
    """Return the canonical form of an email address or raise a validation error.

    The address is stripped, lowercased and NFC-normalized, then structurally
    validated with conservative bounds. No user-controlled string is ever stored
    unnormalized.
    """
    candidate = unicodedata.normalize("NFC", raw.strip()).lower()
    if not candidate or len(candidate) > MAX_TOTAL_LENGTH:
        raise ValidationFailedError([{"field": "email", "message": "invalid email address"}])
    if candidate.count("@") != 1:
        raise ValidationFailedError([{"field": "email", "message": "invalid email address"}])
    local, domain = candidate.split("@", 1)
    if (
        not local
        or len(local) > MAX_LOCAL_LENGTH
        or len(domain) > MAX_DOMAIN_LENGTH
        or not _LOCAL_PART.match(local)
        or not _DOMAIN.match(domain)
    ):
        raise ValidationFailedError([{"field": "email", "message": "invalid email address"}])
    return candidate


def _has_control_chars(value: str) -> bool:
    return any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)


def validate_password(value: str, *, min_length: int, max_length: int) -> str:
    """Validate password bounds and reject control characters (NXS-AUTH-003).

    Length is measured in Unicode code points, bounded before any hashing so a
    pathological megabyte input is rejected in O(1) — the Argon2 verifier is never
    handed unbounded input. The password itself is returned unchanged.
    """
    if not isinstance(value, str):  # pragma: no cover - typing guard
        raise ValidationFailedError([{"field": "password", "message": "must be a string"}])
    if len(value) < min_length or len(value) > max_length:
        raise ValidationFailedError(
            [{"field": "password", "message": f"must be {min_length}-{max_length} characters"}]
        )
    if _has_control_chars(value):
        raise ValidationFailedError(
            [{"field": "password", "message": "must not contain control characters"}]
        )
    return value
