"""Safe SIP control errors without tenant or topology disclosure."""

from nexus_ai.core.errors import NxsError


class SipRouteDeniedError(NxsError):
    code = "NXS_SIP_ROUTE_DENIED"
    status = 403
    title = "SIP routing not authorized"


class SipRouteUnavailableError(NxsError):
    code = "NXS_SIP_ROUTE_UNAVAILABLE"
    status = 503
    title = "SIP routing unavailable"


class SipConflictError(NxsError):
    code = "NXS_SIP_CONFLICT"
    status = 409
    title = "SIP authority conflicts with durable state"
