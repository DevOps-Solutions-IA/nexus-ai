"""Deterministic OTP delivery content (NXS-P10, ADR-0081).

Content is fixed and minimal: there is NO caller-supplied template, no HTML, no
user-controlled header and no template-expression evaluation. Only two safe values are
interpolated — the code (digits only) and the whole-minute TTL — and both are produced
by this subsystem, never by a request.
"""

from __future__ import annotations

from dataclasses import dataclass

from nexus_ai.otp.entities import OtpChannel

_PRODUCT = "Nexus"


@dataclass(frozen=True, slots=True)
class RenderedOtpMessage:
    text: str
    subject: str | None


def _ttl_minutes(ttl_seconds: int) -> int:
    return max(1, round(ttl_seconds / 60))


def render_message(channel: OtpChannel, code: str, ttl_seconds: int) -> RenderedOtpMessage:
    if not code.isdigit():  # defensive: the generator only ever produces digits
        raise ValueError("an OTP code must be digits only")
    minutes = _ttl_minutes(ttl_seconds)
    plural = "minute" if minutes == 1 else "minutes"
    body = (
        f"Your {_PRODUCT} verification code is {code}. "
        f"It expires in {minutes} {plural}. Do not share it with anyone."
    )
    if channel is OtpChannel.EMAIL:
        return RenderedOtpMessage(text=body, subject=f"{_PRODUCT} verification code")
    return RenderedOtpMessage(text=body, subject=None)
