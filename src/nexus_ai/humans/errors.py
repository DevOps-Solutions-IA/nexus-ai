"""Stable RFC 9457 Human Operations error taxonomy."""

from nexus_ai.core.errors import NxsError


class HumanNotFoundError(NxsError):
    code = "NXS_HUMAN_NOT_FOUND"
    status = 404
    title = "Human Operations Resource Not Found"


class HumanInvalidStateError(NxsError):
    code = "NXS_HUMAN_INVALID_STATE"
    status = 409
    title = "Invalid Human Operations State"


class HumanConflictError(NxsError):
    code = "NXS_HUMAN_CONFLICT"
    status = 409
    title = "Human Operations Conflict"


class HumanCapacityExceededError(NxsError):
    code = "NXS_HUMAN_CAPACITY_EXCEEDED"
    status = 409
    title = "Human Agent Capacity Exceeded"


class HumanExecutionFencedError(NxsError):
    code = "NXS_HUMAN_EXECUTION_FENCED"
    status = 409
    title = "Human Execution Fenced"


class HumanBoundaryUnavailableError(NxsError):
    code = "NXS_HUMAN_BOUNDARY_UNAVAILABLE"
    status = 503
    title = "Human Operations Boundary Unavailable"
    retryable = True
