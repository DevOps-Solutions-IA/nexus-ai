"""OTP code generation, the keyed verifier, and destination masking (NXS-P10, ADR-0079).

* **Generation** — a cryptographically secure RNG (:func:`secrets.randbelow`) draws a
  uniform integer in ``[0, 10**length)``, zero-padded. No ``random.Random``, no
  timestamp-derived or sequential codes, no predictable seed.
* **Hashing** — ``HMAC-SHA256(pepper, canonical_context || "\\x1f" || code)``. The
  context binds the Organization, challenge id, purpose, channel and normalized
  destination, so a code is worthless for any other challenge / purpose / tenant even
  if the hash leaks. ``SHA256(code)`` without a secret would be brute-forceable offline
  against the tiny code keyspace — the pepper is mandatory.
* **Verification** — constant-time (:func:`hmac.compare_digest`) over the recomputed
  digest.
* **Masking** — deterministic, leaves enough context for a UX prompt, never the full
  destination.
"""

from __future__ import annotations

import hmac
import secrets
from hashlib import sha256
from uuid import UUID

_UNIT_SEPARATOR = "\x1f"


def generate_code(length: int) -> str:
    """Return a ``length``-digit decimal code drawn from a CSPRNG with a uniform
    distribution over the whole code space."""
    if not 6 <= length <= 10:
        raise ValueError("OTP code length must be between 6 and 10 digits")
    upper = 10**length
    return f"{secrets.randbelow(upper):0{length}d}"


def canonical_context(
    *,
    organization_id: UUID,
    challenge_id: UUID,
    purpose: str,
    channel: str,
    destination: str,
    hash_version: int,
) -> str:
    """The stable, unambiguous binding string mixed into every code hash. Field order
    and the separator are fixed forever for ``hash_version`` 1."""
    return _UNIT_SEPARATOR.join(
        (
            f"nxs-otp:v{hash_version}",
            str(organization_id),
            str(challenge_id),
            purpose,
            channel,
            destination,
        )
    )


def hash_code(pepper: bytes, context: str, code: str) -> str:
    """Return the hex keyed digest for ``code`` under ``context``."""
    message = f"{context}{_UNIT_SEPARATOR}{code}".encode()
    return hmac.new(pepper, message, sha256).hexdigest()


def verify_code(pepper: bytes, context: str, code: str, expected_hash: str) -> bool:
    """Constant-time check of ``code`` against ``expected_hash``."""
    candidate = hash_code(pepper, context, code)
    return hmac.compare_digest(candidate, expected_hash)


def destination_fingerprint(pepper: bytes, channel: str, destination: str) -> str:
    """A stable keyed fingerprint of a normalized destination, used for throttling and
    resend lookups without indexing the raw address in a hot query path."""
    message = f"nxs-otp-dest{_UNIT_SEPARATOR}{channel}{_UNIT_SEPARATOR}{destination}".encode()
    return hmac.new(pepper, message, sha256).hexdigest()


def mask_destination(channel: str, destination: str) -> str:
    """Deterministic partial mask. Email: first char + ``***`` + ``@`` + masked domain
    label + TLD. Phone / other: last 4 kept, everything before replaced with ``*``
    (a leading ``+`` is preserved)."""
    if channel == "EMAIL" and "@" in destination:
        local, _, domain = destination.partition("@")
        local_masked = (local[:1] or "*") + "***"
        labels = domain.split(".")
        if len(labels) >= 2:
            first = labels[0]
            first_masked = (first[:1] or "*") + "***"
            return f"{local_masked}@{'.'.join([first_masked, *labels[1:]])}"
        return f"{local_masked}@***"
    digits = destination.lstrip("+")
    keep = digits[-4:]
    prefix = "+" if destination.startswith("+") else ""
    return f"{prefix}{'*' * max(len(digits) - 4, 2)}{keep}"
