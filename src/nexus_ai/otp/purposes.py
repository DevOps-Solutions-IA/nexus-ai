"""Registered OTP purposes (NXS-P10, ADR-0081).

A purpose is an allow-listed, stable token — never a free-form string. The purpose is
part of the keyed-verifier context, so a code issued for one purpose can never verify a
challenge of another. Only purposes the backend actually needs today are registered; a
downstream product flow (login, password reset, ...) is NOT modelled here — it would
register its own purpose when it exists.

``VERIFY_EMAIL`` / ``VERIFY_PHONE`` are the destination-control checks the OTP mechanism
justifies on its own. ``GENERIC_VERIFICATION`` is the neutral abstraction a caller uses
until a dedicated purpose is warranted.
"""

from __future__ import annotations

from dataclasses import dataclass

from nexus_ai.otp.entities import OtpChannel
from nexus_ai.otp.errors import OtpPurposeInvalidError


@dataclass(frozen=True, slots=True)
class OtpPurpose:
    key: str
    #: Channels this purpose may be delivered over. A subset of the OTP channels.
    allowed_channels: frozenset[OtpChannel]
    description: str


_REGISTRY: dict[str, OtpPurpose] = {
    purpose.key: purpose
    for purpose in (
        OtpPurpose(
            key="GENERIC_VERIFICATION",
            allowed_channels=frozenset({OtpChannel.SMS, OtpChannel.EMAIL}),
            description="Neutral one-time-code verification",
        ),
        OtpPurpose(
            key="VERIFY_EMAIL",
            allowed_channels=frozenset({OtpChannel.EMAIL}),
            description="Confirm control of an email address",
        ),
        OtpPurpose(
            key="VERIFY_PHONE",
            allowed_channels=frozenset({OtpChannel.SMS}),
            description="Confirm control of a phone number",
        ),
    )
}

#: Every registered purpose key, for the contract test and OpenAPI documentation.
REGISTERED_PURPOSES: tuple[str, ...] = tuple(sorted(_REGISTRY))


def resolve_purpose(key: str, channel: OtpChannel) -> OtpPurpose:
    """Return the registered purpose, or raise ``NXS_OTP_PURPOSE_INVALID`` if it is
    unknown or not deliverable over ``channel``."""
    purpose = _REGISTRY.get(key)
    if purpose is None:
        raise OtpPurposeInvalidError("the OTP purpose is not registered")
    if channel not in purpose.allowed_channels:
        raise OtpPurposeInvalidError(
            "this OTP purpose cannot be delivered over the requested channel"
        )
    return purpose
