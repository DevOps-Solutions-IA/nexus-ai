"""Stable NXS application error taxonomy (NXS-ERROR-001).

Public errors expose a stable ``NXS_*`` code, an HTTP status, a safe human title and a
safe detail. Python class names are never part of the contract. Every error renders to
an RFC 9457 Problem Details document via :mod:`nexus_ai.core.problem_details`.
"""

from __future__ import annotations

from typing import Any

ERROR_DOC_BASE = "https://docs.nexus-ai.dev/errors/"


class NxsError(Exception):
    """Base class for every error that may reach an API boundary."""

    code: str = "NXS_CORE_INTERNAL"
    status: int = 500
    title: str = "Internal Server Error"
    retryable: bool = False

    def __init__(
        self,
        detail: str | None = None,
        *,
        extensions: dict[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.detail = detail or self.title
        self.extensions: dict[str, Any] = dict(extensions or {})
        super().__init__(self.detail)
        if cause is not None:
            self.__cause__ = cause

    @property
    def type_uri(self) -> str:
        return f"{ERROR_DOC_BASE}{self.code}"


class InvalidRequestError(NxsError):
    code = "NXS_CORE_INVALID_REQUEST"
    status = 400
    title = "Invalid Request"


class ValidationFailedError(NxsError):
    code = "NXS_CORE_VALIDATION_FAILED"
    status = 422
    title = "Request Validation Failed"

    def __init__(self, errors: list[dict[str, Any]], detail: str | None = None) -> None:
        super().__init__(detail or "One or more request fields are invalid.")
        self.extensions["errors"] = errors


class AuthenticationRequiredError(NxsError):
    code = "NXS_CORE_AUTHENTICATION_REQUIRED"
    status = 401
    title = "Authentication Required"


class PermissionDeniedError(NxsError):
    code = "NXS_CORE_PERMISSION_DENIED"
    status = 403
    title = "Permission Denied"


class NotFoundError(NxsError):
    code = "NXS_CORE_NOT_FOUND"
    status = 404
    title = "Resource Not Found"


class MethodNotAllowedError(NxsError):
    code = "NXS_CORE_METHOD_NOT_ALLOWED"
    status = 405
    title = "Method Not Allowed"


class PayloadTooLargeError(NxsError):
    code = "NXS_CORE_PAYLOAD_TOO_LARGE"
    status = 413
    title = "Payload Too Large"


class RateLimitedError(NxsError):
    code = "NXS_CORE_RATE_LIMITED"
    status = 429
    title = "Too Many Requests"
    retryable = True


class DependencyUnavailableError(NxsError):
    code = "NXS_CORE_DEPENDENCY_UNAVAILABLE"
    status = 503
    title = "Dependency Unavailable"
    retryable = True


class ConfigurationError(NxsError):
    code = "NXS_CORE_CONFIGURATION_ERROR"
    status = 500
    title = "Configuration Error"


class InternalError(NxsError):
    code = "NXS_CORE_INTERNAL"
    status = 500
    title = "Internal Server Error"


PUBLIC_ERRORS: tuple[type[NxsError], ...] = (
    InvalidRequestError,
    ValidationFailedError,
    AuthenticationRequiredError,
    PermissionDeniedError,
    NotFoundError,
    MethodNotAllowedError,
    PayloadTooLargeError,
    RateLimitedError,
    DependencyUnavailableError,
    ConfigurationError,
    InternalError,
)
