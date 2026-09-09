"""Stable messaging error taxonomy (NXS-P09, extends NXS-ERROR-001).

Every failure maps to exactly one stable ``NXS_MSG_*`` code with an HTTP status, a safe
title and a ``retryable`` classification, rendered to RFC 9457 Problem Details. A raw
provider error is NEVER re-exported: its provider code (when safe) is carried in bounded
``extensions``, never its body, headers or any secret.
"""

from __future__ import annotations

from typing import Any

from nexus_ai.core.errors import NxsError


class MessagingAccountNotFoundError(NxsError):
    code = "NXS_MSG_ACCOUNT_NOT_FOUND"
    status = 404
    title = "Messaging Account Not Found"


class MessagingAccountDisabledError(NxsError):
    code = "NXS_MSG_ACCOUNT_DISABLED"
    status = 409
    title = "Messaging Account Disabled"


class MessagingAccountConflictError(NxsError):
    code = "NXS_MSG_ACCOUNT_CONFLICT"
    status = 409
    title = "Messaging Account Conflict"


class MessagingConfigInvalidError(NxsError):
    code = "NXS_MSG_CONFIG_INVALID"
    status = 422
    title = "Invalid Messaging Configuration"


class MessageNotFoundError(NxsError):
    code = "NXS_MSG_MESSAGE_NOT_FOUND"
    status = 404
    title = "Message Not Found"


class MessagingConversationInvalidError(NxsError):
    """The named conversation does not exist in this Organization, is on a different
    channel, or is closed."""

    code = "NXS_MSG_CONVERSATION_INVALID"
    status = 409
    title = "Invalid Conversation For Channel"


class MessagingRecipientInvalidError(NxsError):
    code = "NXS_MSG_RECIPIENT_INVALID"
    status = 422
    title = "Invalid Recipient"


class MessagingPayloadInvalidError(NxsError):
    """An inbound provider payload failed normalization, or an outbound content payload
    violated a channel bound (size, HTML-not-allowed, CRLF / header injection)."""

    code = "NXS_MSG_PAYLOAD_INVALID"
    status = 422
    title = "Invalid Messaging Payload"

    def __init__(
        self, detail: str | None = None, *, errors: list[dict[str, Any]] | None = None
    ) -> None:
        super().__init__(detail or self.title)
        if errors is not None:
            self.extensions["errors"] = errors


class MessagingAuthFailedError(NxsError):
    """Generic inbound authentication failure that is not more specifically a bad
    signature (e.g. a missing/unknown webhook token, wrong provider account)."""

    code = "NXS_MSG_AUTH_FAILED"
    status = 401
    title = "Messaging Webhook Authentication Failed"


class MessagingSignatureInvalidError(NxsError):
    code = "NXS_MSG_SIGNATURE_INVALID"
    status = 401
    title = "Messaging Webhook Signature Invalid"


class MessagingReplayRejectedError(NxsError):
    code = "NXS_MSG_REPLAY_REJECTED"
    status = 409
    title = "Messaging Webhook Replay Rejected"


class MessagingChallengeFailedError(NxsError):
    code = "NXS_MSG_CHALLENGE_FAILED"
    status = 403
    title = "Messaging Webhook Challenge Failed"


class MessagingIdempotencyConflictError(NxsError):
    code = "NXS_MSG_IDEMPOTENCY_CONFLICT"
    status = 409
    title = "Messaging Idempotency Conflict"


class MessagingSendInProgressError(NxsError):
    code = "NXS_MSG_SEND_IN_PROGRESS"
    status = 409
    title = "Messaging Send In Progress"
    retryable = True


class MessagingProviderError(NxsError):
    """The provider rejected the request or returned an error. The provider's own code
    (when safe) is carried in ``extensions.provider_code`` — never its body."""

    code = "NXS_MSG_PROVIDER_ERROR"
    status = 502
    title = "Messaging Provider Error"

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


class MessagingDeliveryFailedError(NxsError):
    code = "NXS_MSG_DELIVERY_FAILED"
    status = 502
    title = "Messaging Delivery Failed"


class MessagingTimeoutError(NxsError):
    """The provider call timed out. Delivery is AMBIGUOUS — a non-idempotent send is
    never blindly retried on this."""

    code = "NXS_MSG_TIMEOUT"
    status = 504
    title = "Messaging Provider Timeout"
    retryable = False


class MessagingRateLimitedError(NxsError):
    code = "NXS_MSG_RATE_LIMITED"
    status = 429
    title = "Messaging Rate Limited"
    retryable = True


class MessagingStateConflictError(NxsError):
    """A delivery status callback attempted an illegal (non-monotonic) transition."""

    code = "NXS_MSG_STATE_CONFLICT"
    status = 409
    title = "Messaging Delivery State Conflict"


#: Every messaging error, for the Problem Details contract and contract tests.
MESSAGING_ERRORS: tuple[type[NxsError], ...] = (
    MessagingAccountNotFoundError,
    MessagingAccountDisabledError,
    MessagingAccountConflictError,
    MessagingConfigInvalidError,
    MessageNotFoundError,
    MessagingConversationInvalidError,
    MessagingRecipientInvalidError,
    MessagingPayloadInvalidError,
    MessagingAuthFailedError,
    MessagingSignatureInvalidError,
    MessagingReplayRejectedError,
    MessagingChallengeFailedError,
    MessagingIdempotencyConflictError,
    MessagingSendInProgressError,
    MessagingProviderError,
    MessagingDeliveryFailedError,
    MessagingTimeoutError,
    MessagingRateLimitedError,
    MessagingStateConflictError,
)
