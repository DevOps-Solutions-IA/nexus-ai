"""Stable voice error taxonomy (NXS-P12, extends NXS-ERROR-001).

Every failure maps to exactly one stable ``NXS_VOICE_*`` code with an HTTP status, a safe
title and a ``retryable`` classification, rendered to RFC 9457 Problem Details. No error
ever carries a provider API key, a signed WebSocket URL, a raw provider frame, an
Authorization header or audio — an adversary learns only the stable failure class.
"""

from __future__ import annotations

from nexus_ai.core.errors import NxsError


class VoiceProviderNotFoundError(NxsError):
    code = "NXS_VOICE_PROVIDER_NOT_FOUND"
    status = 404
    title = "Voice Provider Not Found"


class VoiceAccountNotFoundError(NxsError):
    code = "NXS_VOICE_ACCOUNT_NOT_FOUND"
    status = 404
    title = "Voice Provider Account Not Found"


class VoiceProfileNotFoundError(NxsError):
    code = "NXS_VOICE_PROFILE_NOT_FOUND"
    status = 404
    title = "Voice Profile Not Found"


class VoiceSessionNotFoundError(NxsError):
    code = "NXS_VOICE_SESSION_NOT_FOUND"
    status = 404
    title = "Voice Session Not Found"


class VoiceMediaNotReadyError(NxsError):
    """The referenced NXS-P11 media session does not exist for this call, or is not in a
    state from which a real-time voice stream may be attached."""

    code = "NXS_VOICE_MEDIA_NOT_READY"
    status = 409
    title = "Voice Media Not Ready"


class VoiceConfigInvalidError(NxsError):
    code = "NXS_VOICE_CONFIG_INVALID"
    status = 422
    title = "Invalid Voice Configuration"


class VoiceUnsupportedAudioError(NxsError):
    code = "NXS_VOICE_UNSUPPORTED_AUDIO"
    status = 422
    title = "Unsupported Voice Audio Format"


class VoiceInvalidStateError(NxsError):
    """A requested voice-session transition is not legal from the current state, or would
    mutate a terminal session."""

    code = "NXS_VOICE_INVALID_STATE"
    status = 409
    title = "Invalid Voice Session State"


class VoiceProviderError(NxsError):
    """The provider rejected the request or returned an error. Only a safe provider code
    (when present) is carried in ``extensions.provider_code`` — never its body."""

    code = "NXS_VOICE_PROVIDER_ERROR"
    status = 502
    title = "Voice Provider Error"

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


class VoiceProviderTimeoutError(NxsError):
    """A provider control call timed out. For session creation this is AMBIGUOUS — a
    second external session is never blindly created; the session records a
    pending-reconciliation state."""

    code = "NXS_VOICE_PROVIDER_TIMEOUT"
    status = 504
    title = "Voice Provider Timeout"
    retryable = False


class VoiceConnectionFailedError(NxsError):
    """The real-time transport could not be established (handshake / open timeout /
    network)."""

    code = "NXS_VOICE_CONNECTION_FAILED"
    status = 502
    title = "Voice Connection Failed"
    retryable = True


class VoiceProtocolError(NxsError):
    """A provider frame violated the expected real-time protocol (malformed / oversized /
    out-of-contract)."""

    code = "NXS_VOICE_PROTOCOL_ERROR"
    status = 502
    title = "Voice Protocol Error"


class VoiceWebhookInvalidError(NxsError):
    code = "NXS_VOICE_WEBHOOK_INVALID"
    status = 401
    title = "Voice Webhook Invalid"


class VoiceWebhookReplayError(NxsError):
    code = "NXS_VOICE_WEBHOOK_REPLAY"
    status = 409
    title = "Voice Webhook Replay Rejected"


class VoiceIdempotencyConflictError(NxsError):
    code = "NXS_VOICE_IDEMPOTENCY_CONFLICT"
    status = 409
    title = "Voice Idempotency Conflict"


class VoiceNotAuthorizedError(NxsError):
    """A referenced call / media session / profile is not owned by the authenticated
    Organization, or a provider callback does not resolve to an authorized account."""

    code = "NXS_VOICE_NOT_AUTHORIZED"
    status = 403
    title = "Voice Not Authorized"


#: Every voice error, for the Problem Details contract and contract tests.
VOICE_ERRORS: tuple[type[NxsError], ...] = (
    VoiceProviderNotFoundError,
    VoiceAccountNotFoundError,
    VoiceProfileNotFoundError,
    VoiceSessionNotFoundError,
    VoiceMediaNotReadyError,
    VoiceConfigInvalidError,
    VoiceUnsupportedAudioError,
    VoiceInvalidStateError,
    VoiceProviderError,
    VoiceProviderTimeoutError,
    VoiceConnectionFailedError,
    VoiceProtocolError,
    VoiceWebhookInvalidError,
    VoiceWebhookReplayError,
    VoiceIdempotencyConflictError,
    VoiceNotAuthorizedError,
)


def provider_rest_failure(
    detail: str, *, status_code: int, provider_code: str | None = None
) -> NxsError:
    """Map a provider REST status to the stable voice taxonomy (never its body)."""
    if status_code in (401, 403):
        return VoiceNotAuthorizedError("the voice provider rejected the credential")
    if status_code == 504:
        return VoiceProviderTimeoutError("the voice provider timed out")
    return VoiceProviderError(
        detail,
        provider_code=provider_code,
        provider_status=status_code,
        retryable=status_code in (429, 500, 502, 503),
    )
