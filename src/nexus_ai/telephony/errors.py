"""Stable telephony error taxonomy (NXS-P11, extends NXS-ERROR-001).

Every failure maps to exactly one stable ``NXS_TELEPHONY_*`` code with an HTTP status, a
safe title and a ``retryable`` classification, rendered to RFC 9457 Problem Details. No
error ever carries a SIP password, an ARI credential, a provider token, a raw SDP body
or an Authorization header — an adversary learns only the stable failure class.
"""

from __future__ import annotations

from typing import Any

from nexus_ai.core.errors import NxsError


class TelephonyCallNotFoundError(NxsError):
    code = "NXS_TELEPHONY_CALL_NOT_FOUND"
    status = 404
    title = "Telephony Call Not Found"


class TelephonyAccountNotFoundError(NxsError):
    code = "NXS_TELEPHONY_ACCOUNT_NOT_FOUND"
    status = 404
    title = "Telephony Account Not Found"


class TelephonyNumberNotFoundError(NxsError):
    code = "NXS_TELEPHONY_NUMBER_NOT_FOUND"
    status = 404
    title = "Telephony Phone Number Not Found"


class TelephonyInvalidDestinationError(NxsError):
    code = "NXS_TELEPHONY_INVALID_DESTINATION"
    status = 422
    title = "Invalid Telephony Destination"


class TelephonyInvalidStateError(NxsError):
    """A requested call transition is not legal from the current state, or would mutate
    a terminal call."""

    code = "NXS_TELEPHONY_INVALID_STATE"
    status = 409
    title = "Invalid Telephony Call State"


class TelephonyConfigInvalidError(NxsError):
    code = "NXS_TELEPHONY_CONFIG_INVALID"
    status = 422
    title = "Invalid Telephony Configuration"


class TelephonyProviderError(NxsError):
    """The provider rejected the request or returned an error. The provider's own code
    (when safe) is carried in ``extensions.provider_code`` — never its body."""

    code = "NXS_TELEPHONY_PROVIDER_ERROR"
    status = 502
    title = "Telephony Provider Error"

    def __init__(
        self,
        detail: str | None = None,
        *,
        provider_code: str | None = None,
        provider_status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(detail or self.title)
        self.retryable = retryable
        if provider_code is not None:
            self.extensions["provider_code"] = provider_code
        if provider_status is not None:
            self.extensions["provider_status"] = provider_status


class TelephonyProviderTimeoutError(NxsError):
    """The provider control call timed out. For call creation this is AMBIGUOUS — a
    second call is never blindly created on this."""

    code = "NXS_TELEPHONY_PROVIDER_TIMEOUT"
    status = 504
    title = "Telephony Provider Timeout"
    retryable = False


class TelephonyWebhookInvalidError(NxsError):
    code = "NXS_TELEPHONY_WEBHOOK_INVALID"
    status = 401
    title = "Telephony Webhook Invalid"


class TelephonyWebhookReplayError(NxsError):
    code = "NXS_TELEPHONY_WEBHOOK_REPLAY"
    status = 409
    title = "Telephony Webhook Replay Rejected"


class TelephonyIdempotencyConflictError(NxsError):
    code = "NXS_TELEPHONY_IDEMPOTENCY_CONFLICT"
    status = 409
    title = "Telephony Idempotency Conflict"


class TelephonyNotAuthorizedError(NxsError):
    """The inbound destination does not resolve to an authorized Organization / account,
    or a resource is not owned by the authenticated Organization."""

    code = "NXS_TELEPHONY_NOT_AUTHORIZED"
    status = 403
    title = "Telephony Not Authorized"


class TelephonyDtmfInvalidError(NxsError):
    code = "NXS_TELEPHONY_DTMF_INVALID"
    status = 422
    title = "Invalid DTMF Sequence"


#: Every telephony error, for the Problem Details contract and contract tests.
TELEPHONY_ERRORS: tuple[type[NxsError], ...] = (
    TelephonyCallNotFoundError,
    TelephonyAccountNotFoundError,
    TelephonyNumberNotFoundError,
    TelephonyInvalidDestinationError,
    TelephonyInvalidStateError,
    TelephonyConfigInvalidError,
    TelephonyProviderError,
    TelephonyProviderTimeoutError,
    TelephonyWebhookInvalidError,
    TelephonyWebhookReplayError,
    TelephonyIdempotencyConflictError,
    TelephonyNotAuthorizedError,
    TelephonyDtmfInvalidError,
)


def provider_call_error(
    detail: str, *, status_code: int, provider_code: str | None = None
) -> NxsError:
    """Map a provider HTTP status to the stable telephony taxonomy (never its body)."""
    payload: dict[str, Any] = {"provider_status": status_code}
    if provider_code is not None:
        payload["provider_code"] = provider_code
    if status_code == 504:
        return TelephonyProviderTimeoutError("the telephony provider timed out")
    return TelephonyProviderError(
        detail,
        provider_code=provider_code,
        provider_status=status_code,
        retryable=status_code in (429, 500, 502, 503),
    )
