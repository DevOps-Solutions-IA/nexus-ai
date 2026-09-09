"""Stable OTP error taxonomy (NXS-P10, extends NXS-ERROR-001).

Every failure maps to exactly one stable ``NXS_OTP_*`` code with an HTTP status, a safe
title and a ``retryable`` classification, rendered to RFC 9457 Problem Details. No error
ever carries the OTP code, the code hash, the pepper, or the full destination — an
adversary learns only the stable failure class. ``NXS_OTP_INVALID`` deliberately covers
both a wrong code and a revoked / unknown-but-plausible challenge so verification does
not become an enumeration oracle.
"""

from __future__ import annotations

from nexus_ai.core.errors import NxsError


class OtpChallengeNotFoundError(NxsError):
    code = "NXS_OTP_CHALLENGE_NOT_FOUND"
    status = 404
    title = "OTP Challenge Not Found"


class OtpInvalidError(NxsError):
    """The submitted code did not match, or the challenge is not in a verifiable state
    for a reason that must not be distinguished (revoked, unknown-but-plausible)."""

    code = "NXS_OTP_INVALID"
    status = 401
    title = "OTP Invalid"


class OtpExpiredError(NxsError):
    code = "NXS_OTP_EXPIRED"
    status = 410
    title = "OTP Expired"


class OtpAlreadyUsedError(NxsError):
    code = "NXS_OTP_ALREADY_USED"
    status = 409
    title = "OTP Already Used"


class OtpLockedError(NxsError):
    code = "NXS_OTP_LOCKED"
    status = 423
    title = "OTP Challenge Locked"


class OtpRateLimitedError(NxsError):
    code = "NXS_OTP_RATE_LIMITED"
    status = 429
    title = "OTP Rate Limited"
    retryable = True


class OtpResendTooSoonError(NxsError):
    code = "NXS_OTP_RESEND_TOO_SOON"
    status = 429
    title = "OTP Resend Too Soon"
    retryable = True


class OtpDeliveryFailedError(NxsError):
    code = "NXS_OTP_DELIVERY_FAILED"
    status = 502
    title = "OTP Delivery Failed"


class OtpPurposeInvalidError(NxsError):
    code = "NXS_OTP_PURPOSE_INVALID"
    status = 422
    title = "OTP Purpose Invalid"


class OtpConfigInvalidError(NxsError):
    """The named delivery account is missing, disabled, or on a channel that cannot
    carry an OTP, or the destination is not a valid address for that channel."""

    code = "NXS_OTP_CONFIG_INVALID"
    status = 422
    title = "OTP Configuration Invalid"


class OtpIdempotencyConflictError(NxsError):
    code = "NXS_OTP_IDEMPOTENCY_CONFLICT"
    status = 409
    title = "OTP Idempotency Conflict"


#: Every OTP error, for the Problem Details contract and contract tests.
OTP_ERRORS: tuple[type[NxsError], ...] = (
    OtpChallengeNotFoundError,
    OtpInvalidError,
    OtpExpiredError,
    OtpAlreadyUsedError,
    OtpLockedError,
    OtpRateLimitedError,
    OtpResendTooSoonError,
    OtpDeliveryFailedError,
    OtpPurposeInvalidError,
    OtpConfigInvalidError,
    OtpIdempotencyConflictError,
)
