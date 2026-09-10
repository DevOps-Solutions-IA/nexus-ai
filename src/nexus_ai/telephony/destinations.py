"""Destination canonicalization + SIP safety (NXS-P11, ADR-0084).

The one place a caller-supplied destination becomes a trusted, canonical routing target.
Fails closed on anything ambiguous or unsafe. Reuses the ONE product phone-normalization
vocabulary (NXS-P06 ``normalize_phone``) so a telephony destination and a customer phone
identity resolve to exactly the same E.164 form.

Nexus deliberately separates four concerns that a naive design conflates:

* **display caller ID** — never caller-supplied; derived from an owned PhoneNumber row;
* **provider account** — named by id;
* **routing destination** — the value canonicalized here;
* **internal SIP endpoint** — a bounded alias, resolved from account configuration only.

A user-supplied ``From`` / ``Contact`` / ``Route`` / ``Authorization`` header is never
trusted or forwarded.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum

from nexus_ai.core.errors import IdentityNormalizationError
from nexus_ai.domain.customers.normalization import normalize_phone
from nexus_ai.telephony.errors import TelephonyInvalidDestinationError

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
#: An internal SIP endpoint alias: letters, digits, dot, underscore, hyphen. Never a URI,
#: never a host, never user@host — the account maps the alias to a real endpoint.
_SIP_ALIAS = re.compile(r"^[a-z][a-z0-9._-]{1,63}$")
_MAX_DESTINATION_LEN = 128
#: Anything that looks like SIP-URI / header-injection machinery is refused outright.
_FORBIDDEN_SUBSTRINGS = (
    "\r",
    "\n",
    ";",
    "<",
    ">",
    '"',
    "sip:",
    "sips:",
    "tel:",
    "@",
    " ",
    "\t",
    "%0a",
    "%0d",
    "\\",
)


class DestinationKind(Enum):
    PHONE = "PHONE"
    SIP_ALIAS = "SIP_ALIAS"


@dataclass(frozen=True, slots=True)
class RoutingDestination:
    kind: DestinationKind
    #: Canonical value: E.164 for PHONE, the validated alias for SIP_ALIAS.
    value: str


def canonicalize_destination(raw: str, *, default_country: str) -> RoutingDestination:
    """Canonicalize a caller-supplied destination. Raises
    :class:`TelephonyInvalidDestinationError` on anything malformed, ambiguous, oversized
    or bearing SIP-injection machinery."""
    candidate = unicodedata.normalize("NFKC", raw).strip()
    if not candidate or len(candidate) > _MAX_DESTINATION_LEN:
        raise TelephonyInvalidDestinationError("the destination is empty or too long")
    if _CONTROL.search(candidate):
        raise TelephonyInvalidDestinationError("the destination contains control characters")
    lowered = candidate.lower()
    if any(token in lowered for token in _FORBIDDEN_SUBSTRINGS):
        raise TelephonyInvalidDestinationError(
            "the destination must be a bare E.164 number or an internal endpoint alias"
        )

    if candidate.startswith("+") or candidate.startswith("00") or candidate[0].isdigit():
        try:
            e164 = normalize_phone(candidate, default_country=default_country)
        except IdentityNormalizationError as exc:
            raise TelephonyInvalidDestinationError(
                "the destination is not a valid E.164 phone number"
            ) from exc
        return RoutingDestination(kind=DestinationKind.PHONE, value=e164)

    if _SIP_ALIAS.match(lowered):
        return RoutingDestination(kind=DestinationKind.SIP_ALIAS, value=lowered)

    raise TelephonyInvalidDestinationError(
        "the destination is neither a valid E.164 number nor an internal endpoint alias"
    )


def canonicalize_e164(raw: str, *, default_country: str) -> str:
    """Canonicalize a value that MUST be a phone number (an owned caller-ID number)."""
    result = canonicalize_destination(raw, default_country=default_country)
    if result.kind is not DestinationKind.PHONE:
        raise TelephonyInvalidDestinationError("a phone number in E.164 form is required")
    return result.value


def safe_display_name(raw: str | None) -> str | None:
    """Bound + strip a display name that will accompany caller ID. Control-char-free; no
    quotes / angle brackets that could break a display-name header downstream."""
    if raw is None:
        return None
    cleaned = unicodedata.normalize("NFC", raw).strip()
    cleaned = "".join(ch for ch in cleaned if ch >= " " and ch not in '"<>\r\n')
    return cleaned[:80] or None
