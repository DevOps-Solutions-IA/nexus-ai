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


class TenantContextRequiredError(NxsError):
    code = "NXS_TENANT_CONTEXT_REQUIRED"
    status = 403
    title = "Tenant Context Required"


class TenantContextInvalidError(NxsError):
    code = "NXS_TENANT_CONTEXT_INVALID"
    status = 400
    title = "Invalid Tenant Context"


class TenantScopeMismatchError(NxsError):
    code = "NXS_TENANT_SCOPE_MISMATCH"
    status = 403
    title = "Tenant Scope Mismatch"


class TenantAccessDeniedError(NxsError):
    code = "NXS_TENANT_ACCESS_DENIED"
    status = 403
    title = "Tenant Access Denied"


class OrganizationNotFoundError(NxsError):
    code = "NXS_ORG_NOT_FOUND"
    status = 404
    title = "Organization Not Found"


class OrganizationConflictError(NxsError):
    code = "NXS_ORG_CONFLICT"
    status = 409
    title = "Organization Conflict"


class OrganizationInvalidStateError(NxsError):
    code = "NXS_ORG_INVALID_STATE"
    status = 409
    title = "Invalid Organization State Transition"


class OrganizationInactiveError(NxsError):
    code = "NXS_ORG_INACTIVE"
    status = 403
    title = "Organization Not Active"


class OrganizationVersionConflictError(NxsError):
    code = "NXS_ORG_VERSION_CONFLICT"
    status = 409
    title = "Organization Version Conflict"
    retryable = True


class AuthenticationFailedError(NxsError):
    """Generic login/refresh failure. Identical for unknown email, bad password, revoked
    session and disabled identity — no caller-visible distinction, no enumeration."""

    code = "NXS_AUTH_CREDENTIALS_REJECTED"
    status = 401
    title = "Authentication Failed"


class TokenValidationError(NxsError):
    """A presented access token failed cryptographic or claim validation (fail closed)."""

    code = "NXS_AUTH_TOKEN_INVALID"
    status = 401
    title = "Invalid Token"


class SessionRevokedError(NxsError):
    code = "NXS_AUTH_SESSION_REVOKED"
    status = 401
    title = "Session Revoked"


class MembershipRequiredError(NxsError):
    code = "NXS_AUTH_MEMBERSHIP_REQUIRED"
    status = 403
    title = "Organization Membership Required"


class MembershipInactiveError(NxsError):
    code = "NXS_AUTH_MEMBERSHIP_INACTIVE"
    status = 403
    title = "Organization Membership Not Active"


class MembershipConflictError(NxsError):
    code = "NXS_AUTH_MEMBERSHIP_CONFLICT"
    status = 409
    title = "Membership Conflict"


class UserConflictError(NxsError):
    code = "NXS_AUTH_USER_CONFLICT"
    status = 409
    title = "User Conflict"


class UserInactiveError(NxsError):
    code = "NXS_AUTH_USER_INACTIVE"
    status = 403
    title = "User Not Active"


class MultipleOrganizationsError(NxsError):
    code = "NXS_AUTH_MULTIPLE_ORGS"
    status = 400
    title = "Organization Selection Required"


# --- Provisioning and dashboard (NXS-P05) ---------------------------------------------


class IdempotencyKeyConflictError(NxsError):
    """The same idempotency key was replayed with a different request payload."""

    code = "NXS_PROV_IDEMPOTENCY_CONFLICT"
    status = 409
    title = "Idempotency Conflict"


class ProvisioningInProgressError(NxsError):
    """An idempotent provisioning request is still executing elsewhere."""

    code = "NXS_PROV_IN_PROGRESS"
    status = 409
    title = "Provisioning In Progress"
    retryable = True


class ProvisioningFailedError(NxsError):
    """Replay of a terminally failed provisioning request. Carries the original,
    sanitized error code — the caller must use a new idempotency key to retry."""

    code = "NXS_PROV_FAILED"
    status = 409
    title = "Provisioning Failed"

    def __init__(self, detail: str | None = None, *, original_error_code: str) -> None:
        super().__init__(
            detail or self.title, extensions={"original_error_code": original_error_code}
        )


class ProvisioningOwnerUnavailableError(NxsError):
    """The requested initial owner does not exist or is not an ACTIVE user."""

    code = "NXS_PROV_OWNER_UNAVAILABLE"
    status = 422
    title = "Initial Owner Unavailable"


class ProvisioningStateConflictError(NxsError):
    """A provisioning invariant is violated (e.g. an already-provisioned Organization
    receiving a destructive re-provision attempt)."""

    code = "NXS_PROV_STATE_CONFLICT"
    status = 409
    title = "Provisioning State Conflict"


class IdentityConflictError(NxsError):
    """A canonical identity already belongs to a different Customer in this Organization."""

    code = "NXS_CUSTOMER_IDENTITY_CONFLICT"
    status = 409
    title = "Customer Identity Conflict"


class IdentityNormalizationError(NxsError):
    """An identity value failed deterministic normalization/validation."""

    code = "NXS_CUSTOMER_IDENTITY_INVALID"
    status = 422
    title = "Invalid Customer Identity"


class UnsupportedIdentityTypeError(NxsError):
    code = "NXS_CUSTOMER_IDENTITY_UNSUPPORTED"
    status = 422
    title = "Unsupported Identity Type"


class CustomerNotFoundError(NxsError):
    code = "NXS_CUSTOMER_NOT_FOUND"
    status = 404
    title = "Customer Not Found"


class ConversationNotFoundError(NxsError):
    code = "NXS_CONVERSATION_NOT_FOUND"
    status = 404
    title = "Conversation Not Found"


class ConversationStateConflictError(NxsError):
    code = "NXS_CONVERSATION_STATE_CONFLICT"
    status = 409
    title = "Conversation State Conflict"


class ConversationExternalKeyConflictError(NxsError):
    code = "NXS_CONVERSATION_EXTERNAL_CONFLICT"
    status = 409
    title = "Conversation External Key Conflict"


class DashboardSchemaError(NxsError):
    """Base for deterministic dashboard schema failures."""

    code = "NXS_DASH_SCHEMA_INVALID"
    status = 422
    title = "Invalid Dashboard Schema"


class UnknownWidgetError(NxsError):
    code = "NXS_DASH_UNKNOWN_WIDGET"
    status = 409
    title = "Unknown Dashboard Widget"


class UnsupportedDashboardVersionError(NxsError):
    code = "NXS_DASH_UNSUPPORTED_VERSION"
    status = 409
    title = "Unsupported Dashboard Schema Version"


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
    TenantContextRequiredError,
    TenantContextInvalidError,
    TenantScopeMismatchError,
    TenantAccessDeniedError,
    OrganizationNotFoundError,
    OrganizationConflictError,
    OrganizationInvalidStateError,
    OrganizationInactiveError,
    OrganizationVersionConflictError,
    AuthenticationFailedError,
    TokenValidationError,
    SessionRevokedError,
    MembershipRequiredError,
    MembershipInactiveError,
    MembershipConflictError,
    UserConflictError,
    UserInactiveError,
    MultipleOrganizationsError,
    IdempotencyKeyConflictError,
    ProvisioningInProgressError,
    ProvisioningFailedError,
    ProvisioningOwnerUnavailableError,
    ProvisioningStateConflictError,
    DashboardSchemaError,
    UnknownWidgetError,
    UnsupportedDashboardVersionError,
    IdentityConflictError,
    IdentityNormalizationError,
    UnsupportedIdentityTypeError,
    CustomerNotFoundError,
    ConversationNotFoundError,
    ConversationStateConflictError,
    ConversationExternalKeyConflictError,
)
