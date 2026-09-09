"""OTP Services (NXS-P10: NXS-OTP-001).

A secure, deterministic, tenant-isolated one-time-code subsystem for generic
authentication / verification flows. Codes are generated with a CSPRNG, stored only as
a keyed HMAC (never plaintext), bound to their Organization + purpose + destination +
challenge id, expire, are single-use, are attempt-limited and locked, and are throttled
per subject. Delivery is orchestrated exclusively through the NXS-P09 messaging service
— this package never opens a socket, never speaks a provider protocol and never holds a
provider credential.

P10 does NOT own OTP-specific product flows (login, password reset, ...): it exposes a
generic, purpose-registered mechanism that a later phase consumes.
"""

from __future__ import annotations
