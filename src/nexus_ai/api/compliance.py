"""Authenticated bounded compliance APIs; authority is rechecked by the service."""

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from nexus_ai.api.auth_deps import LivePrincipalDep
from nexus_ai.api.dependencies import get_resources
from nexus_ai.compliance.contracts import (
    ApprovalInput,
    ClaimInput,
    ExecutionFence,
    HoldInput,
    PolicyInput,
    RetentionInput,
    SubjectRequestInput,
    VerificationInput,
)
from nexus_ai.compliance.service import ComplianceService

compliance_router = APIRouter(prefix="/compliance", tags=["compliance"])


def _service(request: Request) -> ComplianceService:
    resources = get_resources(request)
    return ComplianceService(
        resources.database,
        resources.event_platform.publisher,
        service_name=resources.settings.service_name,
    )


@compliance_router.post("/policies")
async def create_policy(
    payload: PolicyInput, principal: LivePrincipalDep, request: Request
) -> dict[str, Any]:
    return await _service(request).create_policy(principal, payload)


@compliance_router.post("/policies/{identity}/activate")
async def activate_policy(
    identity: UUID, principal: LivePrincipalDep, request: Request
) -> dict[str, Any]:
    return await _service(request).activate_policy(principal, identity)


@compliance_router.post("/policies/{identity}/retire", status_code=204)
async def retire_policy(identity: UUID, principal: LivePrincipalDep, request: Request) -> Response:
    await _service(request).retire_policy(principal, identity)
    return Response(status_code=204)


@compliance_router.post("/holds")
async def create_hold(
    payload: HoldInput, principal: LivePrincipalDep, request: Request
) -> dict[str, Any]:
    return await _service(request).create_hold(principal, payload)


@compliance_router.post("/holds/{identity}/release", status_code=204)
async def release_hold(identity: UUID, principal: LivePrincipalDep, request: Request) -> Response:
    await _service(request).release_hold(principal, identity)
    return Response(status_code=204)


@compliance_router.post("/requests")
async def create_request(
    payload: SubjectRequestInput, principal: LivePrincipalDep, request: Request
) -> dict[str, Any]:
    return await _service(request).create_request(principal, payload)


@compliance_router.get("/requests")
async def list_requests(
    principal: LivePrincipalDep,
    request: Request,
    after: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[dict[str, Any]]:
    return await _service(request).list_requests(principal, after=after, limit=limit)


@compliance_router.get("/requests/{identity}")
async def get_request(
    identity: UUID, principal: LivePrincipalDep, request: Request
) -> dict[str, Any]:
    return await _service(request).get_request(principal, identity)


@compliance_router.post("/requests/{identity}/verify")
async def verify_request(
    identity: UUID, payload: VerificationInput, principal: LivePrincipalDep, request: Request
) -> dict[str, Any]:
    return await _service(request).verify_request(principal, identity, payload)


@compliance_router.post("/requests/{identity}/plan")
async def plan_request(
    identity: UUID, principal: LivePrincipalDep, request: Request
) -> dict[str, Any]:
    return await _service(request).plan_request(principal, identity)


@compliance_router.post("/requests/{identity}/decisions/{decision}")
async def end_request(
    identity: UUID,
    decision: Literal["DENIED", "CANCELLED", "EXPIRED"],
    principal: LivePrincipalDep,
    request: Request,
) -> dict[str, Any]:
    return await _service(request).end_request(principal, identity, decision)


@compliance_router.post("/retention/evaluate")
async def evaluate_retention(
    payload: RetentionInput, principal: LivePrincipalDep, request: Request
) -> dict[str, Any]:
    return await _service(request).evaluate_retention(principal, payload)


@compliance_router.post("/plans/{identity}/approve")
async def approve(
    identity: UUID, payload: ApprovalInput, principal: LivePrincipalDep, request: Request
) -> dict[str, Any]:
    return await _service(request).approve(principal, identity, payload)


@compliance_router.post("/plans/{identity}/claim")
async def claim(
    identity: UUID, payload: ClaimInput, principal: LivePrincipalDep, request: Request
) -> dict[str, Any]:
    return await _service(request).claim(principal, identity, payload)


@compliance_router.post("/plans/{identity}/execute")
async def execute(
    identity: UUID, payload: ExecutionFence, principal: LivePrincipalDep, request: Request
) -> dict[str, Any]:
    return await _service(request).execute(principal, identity, payload)
