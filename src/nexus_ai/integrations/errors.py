"""Stable Integration Hub error taxonomy (NXS-INT-001, extends NXS-ERROR-001).

Every failure the Integration Hub can produce maps to exactly one stable ``NXS_INT_*``
code with an HTTP status, a safe human title and a ``retryable`` classification. These
render to RFC 9457 Problem Details through :mod:`nexus_ai.core.problem_details` like every
other :class:`~nexus_ai.core.errors.NxsError`. Python class names are never part of the
contract.

The taxonomy is deliberately coarse and closed: an untrusted upstream response, a policy
rejection, a transport fault and a configuration mistake are each ONE category, so callers
(and the future Tool Engine) branch on a small, stable set. Provider-specific detail lives
only in bounded ``extensions`` — never in a new error class per provider.
"""

from __future__ import annotations

from typing import Any

from nexus_ai.core.errors import NxsError

# --- configuration / registry (caller or operator mistake) ----------------------------


class IntegrationNotFoundError(NxsError):
    code = "NXS_INT_NOT_FOUND"
    status = 404
    title = "Integration Not Found"


class IntegrationOperationNotFoundError(NxsError):
    code = "NXS_INT_OPERATION_NOT_FOUND"
    status = 404
    title = "Integration Operation Not Found"


class IntegrationConflictError(NxsError):
    code = "NXS_INT_CONFLICT"
    status = 409
    title = "Integration Conflict"


class IntegrationDisabledError(NxsError):
    code = "NXS_INT_DISABLED"
    status = 409
    title = "Integration Not Active"


class IntegrationConfigInvalidError(NxsError):
    code = "NXS_INT_CONFIG_INVALID"
    status = 422
    title = "Invalid Integration Configuration"


class IntegrationAuthProfileInvalidError(NxsError):
    code = "NXS_INT_AUTH_PROFILE_INVALID"
    status = 422
    title = "Invalid Integration Auth Profile"


class IntegrationOperationInputInvalidError(NxsError):
    code = "NXS_INT_OPERATION_INPUT_INVALID"
    status = 422
    title = "Invalid Integration Operation Input"

    def __init__(
        self, detail: str | None = None, *, errors: list[dict[str, Any]] | None = None
    ) -> None:
        super().__init__(detail or self.title)
        if errors is not None:
            self.extensions["errors"] = errors


class IntegrationOpenApiInvalidError(NxsError):
    code = "NXS_INT_OPENAPI_INVALID"
    status = 422
    title = "Rejected OpenAPI Document"


class IntegrationGraphQLInvalidError(NxsError):
    code = "NXS_INT_GRAPHQL_INVALID"
    status = 422
    title = "Rejected GraphQL Document"


# --- destination policy (SSRF) --------------------------------------------------------


class IntegrationDestinationBlockedError(NxsError):
    """A URL failed the SSRF destination policy at configuration OR execution time."""

    code = "NXS_INT_DESTINATION_BLOCKED"
    status = 422
    title = "Integration Destination Blocked"

    def __init__(self, detail: str | None = None, *, reason: str | None = None) -> None:
        super().__init__(detail or self.title)
        if reason is not None:
            self.extensions["reason"] = reason


# --- credential / vault seam ---------------------------------------------------------


class IntegrationCredentialUnavailableError(NxsError):
    """The credential reference does not resolve to usable secret material."""

    code = "NXS_INT_CREDENTIAL_UNAVAILABLE"
    status = 409
    title = "Integration Credential Unavailable"


# --- transport faults (governed executor) ------------------------------------------


class IntegrationTimeoutError(NxsError):
    code = "NXS_INT_TIMEOUT"
    status = 504
    title = "Integration Request Timed Out"
    retryable = True


class IntegrationConnectionError(NxsError):
    code = "NXS_INT_CONNECTION_ERROR"
    status = 502
    title = "Integration Connection Failed"
    retryable = True


class IntegrationTlsError(NxsError):
    code = "NXS_INT_TLS_ERROR"
    status = 502
    title = "Integration TLS Verification Failed"


class IntegrationRedirectBlockedError(NxsError):
    code = "NXS_INT_REDIRECT_BLOCKED"
    status = 502
    title = "Integration Redirect Blocked"


class IntegrationResponseTooLargeError(NxsError):
    code = "NXS_INT_RESPONSE_TOO_LARGE"
    status = 502
    title = "Integration Response Too Large"


# --- untrusted response validation -------------------------------------------------


class IntegrationResponseInvalidError(NxsError):
    """An upstream response failed status/content-type/parse/output-schema validation."""

    code = "NXS_INT_RESPONSE_INVALID"
    status = 502
    title = "Invalid Integration Response"

    def __init__(self, detail: str | None = None, *, reason: str | None = None) -> None:
        super().__init__(detail or self.title)
        if reason is not None:
            self.extensions["reason"] = reason


class IntegrationUpstreamClientError(NxsError):
    """The upstream returned a 4xx status for a well-formed request (not retryable)."""

    code = "NXS_INT_UPSTREAM_CLIENT_ERROR"
    status = 502
    title = "Integration Upstream Rejected the Request"

    def __init__(self, detail: str | None = None, *, upstream_status: int | None = None) -> None:
        super().__init__(detail or self.title)
        if upstream_status is not None:
            self.extensions["upstream_status"] = upstream_status


class IntegrationUpstreamServerError(NxsError):
    """The upstream returned a 5xx status (retryable under policy)."""

    code = "NXS_INT_UPSTREAM_SERVER_ERROR"
    status = 502
    title = "Integration Upstream Failed"
    retryable = True

    def __init__(self, detail: str | None = None, *, upstream_status: int | None = None) -> None:
        super().__init__(detail or self.title)
        if upstream_status is not None:
            self.extensions["upstream_status"] = upstream_status


class IntegrationUpstreamRateLimitedError(NxsError):
    """The upstream returned 429. Normalized; carries ``retry_after_seconds`` when known."""

    code = "NXS_INT_UPSTREAM_RATE_LIMITED"
    status = 429
    title = "Integration Upstream Rate Limited"
    retryable = True

    def __init__(
        self, detail: str | None = None, *, retry_after_seconds: float | None = None
    ) -> None:
        super().__init__(detail or self.title)
        if retry_after_seconds is not None:
            self.extensions["retry_after_seconds"] = retry_after_seconds


# --- Hub-side flow control --------------------------------------------------------


class IntegrationCircuitOpenError(NxsError):
    code = "NXS_INT_CIRCUIT_OPEN"
    status = 503
    title = "Integration Circuit Open"
    retryable = True


class IntegrationOutboundRateLimitedError(NxsError):
    """The Hub's own per-organization outbound rate limit rejected the call."""

    code = "NXS_INT_OUTBOUND_RATE_LIMITED"
    status = 429
    title = "Integration Outbound Rate Limited"
    retryable = True

    def __init__(
        self, detail: str | None = None, *, retry_after_seconds: float | None = None
    ) -> None:
        super().__init__(detail or self.title)
        if retry_after_seconds is not None:
            self.extensions["retry_after_seconds"] = retry_after_seconds


class IntegrationIdempotencyConflictError(NxsError):
    """The same idempotency key was replayed with a different request fingerprint."""

    code = "NXS_INT_IDEMPOTENCY_CONFLICT"
    status = 409
    title = "Integration Idempotency Conflict"


class IntegrationExecutionInProgressError(NxsError):
    """An execution with this idempotency key is still running elsewhere. Retryable."""

    code = "NXS_INT_EXECUTION_IN_PROGRESS"
    status = 409
    title = "Integration Execution In Progress"
    retryable = True


class IntegrationExecutionFailedError(NxsError):
    """A generic terminal execution failure that fits no more specific category."""

    code = "NXS_INT_EXECUTION_FAILED"
    status = 502
    title = "Integration Execution Failed"


# --- inbound webhooks -----------------------------------------------------------


class WebhookEndpointNotFoundError(NxsError):
    code = "NXS_INT_WEBHOOK_ENDPOINT_NOT_FOUND"
    status = 404
    title = "Webhook Endpoint Not Found"


class WebhookSignatureInvalidError(NxsError):
    """The inbound webhook signature failed constant-time verification, or was absent
    where a verifier is configured. Unsigned is never treated as verified."""

    code = "NXS_INT_WEBHOOK_SIGNATURE_INVALID"
    status = 401
    title = "Invalid Webhook Signature"


class WebhookReplayError(NxsError):
    """The webhook timestamp is outside the tolerance window, or its id was already
    processed (durable dedup)."""

    code = "NXS_INT_WEBHOOK_REPLAY"
    status = 409
    title = "Webhook Replay Rejected"


class WebhookPayloadRejectedError(NxsError):
    code = "NXS_INT_WEBHOOK_PAYLOAD_REJECTED"
    status = 422
    title = "Webhook Payload Rejected"


#: Every Integration Hub error, registered into the public Problem Details contract.
INTEGRATION_ERRORS: tuple[type[NxsError], ...] = (
    IntegrationNotFoundError,
    IntegrationOperationNotFoundError,
    IntegrationConflictError,
    IntegrationDisabledError,
    IntegrationConfigInvalidError,
    IntegrationAuthProfileInvalidError,
    IntegrationOperationInputInvalidError,
    IntegrationOpenApiInvalidError,
    IntegrationGraphQLInvalidError,
    IntegrationDestinationBlockedError,
    IntegrationCredentialUnavailableError,
    IntegrationTimeoutError,
    IntegrationConnectionError,
    IntegrationTlsError,
    IntegrationRedirectBlockedError,
    IntegrationResponseTooLargeError,
    IntegrationResponseInvalidError,
    IntegrationUpstreamClientError,
    IntegrationUpstreamServerError,
    IntegrationUpstreamRateLimitedError,
    IntegrationCircuitOpenError,
    IntegrationOutboundRateLimitedError,
    IntegrationIdempotencyConflictError,
    IntegrationExecutionInProgressError,
    IntegrationExecutionFailedError,
    WebhookEndpointNotFoundError,
    WebhookSignatureInvalidError,
    WebhookReplayError,
    WebhookPayloadRejectedError,
)
