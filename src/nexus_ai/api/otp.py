"""OTP Services API surface (NXS-P10).

Governed operations only: issue a one-time code, verify a submitted code, resend, and
read safe challenge metadata. There is deliberately NO way to read the code, the code
hash or the pepper, no raw provider / template / header surface, and every response
carries only a *masked* destination.

Every route is authenticated and permission-checked. A ``login``-style unauthenticated
flow is intentionally NOT exposed here — no such consumer exists yet, and P10 ships the
generic governed mechanism a later phase would build on (ADR-0083).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request, status

from nexus_ai.api.authorization import OtpIssueDep, OtpReadDep, OtpVerifyDep
from nexus_ai.api.dependencies import TenantContextDep, get_resources
from nexus_ai.otp.entities import (
    IssueOtpRequest,
    IssueOtpResult,
    OtpChallengeView,
    VerifyOtpRequest,
    VerifyOtpResult,
)

otp_router = APIRouter(prefix="/otp", tags=["otp"])


@otp_router.post("/challenges", response_model=IssueOtpResult, status_code=status.HTTP_201_CREATED)
async def issue_challenge(
    payload: IssueOtpRequest,
    context: TenantContextDep,
    _authorized: OtpIssueDep,
    request: Request,
) -> IssueOtpResult:
    return await get_resources(request).otp.issue(context.organization_id, payload)


@otp_router.get("/challenges/{challenge_id}", response_model=OtpChallengeView)
async def get_challenge(
    challenge_id: UUID,
    context: TenantContextDep,
    _authorized: OtpReadDep,
    request: Request,
) -> OtpChallengeView:
    return await get_resources(request).otp.get_challenge(context.organization_id, challenge_id)


@otp_router.post("/challenges/{challenge_id}/verify", response_model=VerifyOtpResult)
async def verify_challenge(
    challenge_id: UUID,
    payload: VerifyOtpRequest,
    context: TenantContextDep,
    _authorized: OtpVerifyDep,
    request: Request,
) -> VerifyOtpResult:
    return await get_resources(request).otp.verify(context.organization_id, challenge_id, payload)


@otp_router.post(
    "/challenges/{challenge_id}/resend",
    response_model=IssueOtpResult,
    status_code=status.HTTP_201_CREATED,
)
async def resend_challenge(
    challenge_id: UUID,
    context: TenantContextDep,
    _authorized: OtpIssueDep,
    request: Request,
) -> IssueOtpResult:
    return await get_resources(request).otp.resend(context.organization_id, challenge_id)
