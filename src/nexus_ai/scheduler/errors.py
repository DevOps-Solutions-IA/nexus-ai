"""Stable Scheduler RFC 9457 error taxonomy."""

from nexus_ai.core.errors import NxsError


class ScheduleNotFoundError(NxsError):
    code = "NXS_SCHED_NOT_FOUND"
    status = 404
    title = "Schedule Not Found"


class OccurrenceNotFoundError(NxsError):
    code = "NXS_SCHED_OCCURRENCE_NOT_FOUND"
    status = 404
    title = "Schedule Occurrence Not Found"


class ScheduleInvalidStateError(NxsError):
    code = "NXS_SCHED_INVALID_STATE"
    status = 409
    title = "Invalid Schedule State"


class ScheduleConflictError(NxsError):
    code = "NXS_SCHED_CONFLICT"
    status = 409
    title = "Schedule Conflict"


class ScheduleInvalidRecurrenceError(NxsError):
    code = "NXS_SCHED_INVALID_RECURRENCE"
    status = 422
    title = "Invalid Schedule Recurrence"


class ScheduleTargetRejectedError(NxsError):
    code = "NXS_SCHED_TARGET_REJECTED"
    status = 422
    title = "Schedule Target Rejected"


class ScheduleExecutionFencedError(NxsError):
    code = "NXS_SCHED_EXECUTION_FENCED"
    status = 409
    title = "Schedule Execution Fenced"


class ScheduleDispatchFailedError(NxsError):
    code = "NXS_SCHED_DISPATCH_FAILED"
    status = 503
    title = "Schedule Dispatch Failed"
    retryable = True
