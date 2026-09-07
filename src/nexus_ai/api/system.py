"""System and health endpoints (NXS-API-001, NXS-HEALTH-001).

Liveness never touches a dependency. Readiness aggregates typed probes and returns
``200 READY`` or ``503 NOT_READY`` with safe dependency detail. The pre-P01 ``/health``
and ``/version`` endpoints are preserved for backward compatibility.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel

from nexus_ai import __version__
from nexus_ai.api.dependencies import BootstrapSettingsDep, MetadataDep, ReadinessDep
from nexus_ai.core.metadata import ServiceMetadata

probes_router = APIRouter(tags=["system"])
system_router = APIRouter(prefix="/system", tags=["system"])


class LivenessResponse(BaseModel):
    status: Literal["alive"] = "alive"


class LegacyHealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class LegacyVersionResponse(BaseModel):
    product: str
    version: str


class ReadinessResponse(BaseModel):
    status: Literal["READY", "NOT_READY"]
    dependencies: list[dict[str, object]]


@probes_router.get("/health/live", response_model=LivenessResponse, summary="Liveness probe")
async def liveness() -> LivenessResponse:
    return LivenessResponse()


@probes_router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    responses={503: {"model": ReadinessResponse}},
)
async def readiness(report: ReadinessDep, response: Response) -> ReadinessResponse:
    if not report.ready:
        response.status_code = 503
    return ReadinessResponse(
        status="READY" if report.ready else "NOT_READY",
        dependencies=report.dependency_payloads(),
    )


@probes_router.get(
    "/health",
    response_model=LegacyHealthResponse,
    summary="Legacy health endpoint (pre-P01 compatibility)",
)
async def legacy_health() -> LegacyHealthResponse:
    return LegacyHealthResponse()


@probes_router.get(
    "/version",
    response_model=LegacyVersionResponse,
    summary="Legacy version endpoint (pre-P01 compatibility)",
)
async def legacy_version(settings: BootstrapSettingsDep) -> LegacyVersionResponse:
    return LegacyVersionResponse(product=settings.product, version=__version__)


@system_router.get("/version", response_model=ServiceMetadata, summary="Service metadata")
async def service_version(metadata: MetadataDep) -> ServiceMetadata:
    return metadata


@system_router.get(
    "/health",
    response_model=ReadinessResponse,
    summary="Dependency health",
    responses={503: {"model": ReadinessResponse}},
)
async def system_health(report: ReadinessDep, response: Response) -> ReadinessResponse:
    if not report.ready:
        response.status_code = 503
    return ReadinessResponse(
        status="READY" if report.ready else "NOT_READY",
        dependencies=report.dependency_payloads(),
    )
