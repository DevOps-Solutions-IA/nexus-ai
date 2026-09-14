"""Stable RFC 9457 Campaign error taxonomy."""

from nexus_ai.core.errors import NxsError


class CampaignNotFoundError(NxsError):
    code = "NXS_CAMPAIGN_NOT_FOUND"
    status = 404
    title = "Campaign Not Found"


class CampaignRunNotFoundError(NxsError):
    code = "NXS_CAMPAIGN_RUN_NOT_FOUND"
    status = 404
    title = "Campaign Run Not Found"


class CampaignInvalidStateError(NxsError):
    code = "NXS_CAMPAIGN_INVALID_STATE"
    status = 409
    title = "Invalid Campaign State"


class CampaignConflictError(NxsError):
    code = "NXS_CAMPAIGN_CONFLICT"
    status = 409
    title = "Campaign Conflict"


class CampaignInvalidAudienceError(NxsError):
    code = "NXS_CAMPAIGN_INVALID_AUDIENCE"
    status = 422
    title = "Invalid Campaign Audience"


class CampaignEligibilityDeniedError(NxsError):
    code = "NXS_CAMPAIGN_ELIGIBILITY_DENIED"
    status = 409
    title = "Campaign Recipient Ineligible"


class CampaignExecutionFencedError(NxsError):
    code = "NXS_CAMPAIGN_EXECUTION_FENCED"
    status = 409
    title = "Campaign Execution Fenced"


class CampaignDispatchFailedError(NxsError):
    code = "NXS_CAMPAIGN_DISPATCH_FAILED"
    status = 503
    title = "Campaign Dispatch Failed"
    retryable = True
