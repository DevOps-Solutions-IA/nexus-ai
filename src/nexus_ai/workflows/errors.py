"""Stable Workflow Engine RFC 9457 error taxonomy."""

from nexus_ai.core.errors import NxsError


class WorkflowNotFoundError(NxsError):
    code = "NXS_WORKFLOW_NOT_FOUND"
    status = 404
    title = "Workflow Not Found"


class WorkflowVersionNotFoundError(NxsError):
    code = "NXS_WORKFLOW_VERSION_NOT_FOUND"
    status = 404
    title = "Workflow Version Not Found"


class WorkflowRunNotFoundError(NxsError):
    code = "NXS_WORKFLOW_RUN_NOT_FOUND"
    status = 404
    title = "Workflow Run Not Found"


class WorkflowInvalidDefinitionError(NxsError):
    code = "NXS_WORKFLOW_INVALID_DEFINITION"
    status = 422
    title = "Invalid Workflow Definition"


class WorkflowInvalidStateError(NxsError):
    code = "NXS_WORKFLOW_INVALID_STATE"
    status = 409
    title = "Invalid Workflow State"


class WorkflowConflictError(NxsError):
    code = "NXS_WORKFLOW_CONFLICT"
    status = 409
    title = "Workflow Conflict"


class WorkflowExecutionFencedError(NxsError):
    code = "NXS_WORKFLOW_EXECUTION_FENCED"
    status = 409
    title = "Workflow Execution Fenced"


class WorkflowStepFailedError(NxsError):
    code = "NXS_WORKFLOW_STEP_FAILED"
    status = 502
    title = "Workflow Step Failed"
