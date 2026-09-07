"""Typed, explicit dependency injection (NXS-API-001, no service locator).

Every dependency is resolved from ``request.app.state`` and is replaceable in tests by
overriding the FastAPI dependency or constructing the app with test settings.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai.api.contracts import RequestMetadata, parse_idempotency_key
from nexus_ai.core.config import Settings
from nexus_ai.core.context import current_context
from nexus_ai.core.errors import InternalError
from nexus_ai.core.health import HealthReport
from nexus_ai.core.lifecycle import ApplicationLifespan, Resources
from nexus_ai.core.metadata import ServiceMetadata
from nexus_ai.infrastructure.cache import Cache
from nexus_ai.infrastructure.messaging import Messaging


def get_lifespan(request: Request) -> ApplicationLifespan:
    lifespan = getattr(request.app.state, "lifespan", None)
    if not isinstance(lifespan, ApplicationLifespan):
        raise InternalError("application lifespan is not available")
    return lifespan


def get_resources(request: Request) -> Resources:
    return get_lifespan(request).resources


def get_bootstrap_settings(request: Request) -> Settings:
    """Settings available before the lifespan runs (used by legacy compatibility routes)."""
    settings = getattr(request.app.state, "settings", None)
    if not isinstance(settings, Settings):
        raise InternalError("application settings are not available")
    return settings


def get_settings(request: Request) -> Settings:
    return get_resources(request).settings


def get_metadata(request: Request) -> ServiceMetadata:
    return get_resources(request).metadata


def get_cache(request: Request) -> Cache:
    return get_resources(request).cache


def get_messaging(request: Request) -> Messaging:
    return get_resources(request).messaging


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with get_resources(request).database.session() as session:
        yield session


async def get_readiness_report(request: Request) -> HealthReport:
    return await get_resources(request).readiness.evaluate()


def get_request_metadata(request: Request) -> RequestMetadata:
    context = current_context()
    idempotency_key = parse_idempotency_key(request.headers.get("Idempotency-Key"))
    if context is None:
        raise InternalError("request context is not available")
    return RequestMetadata(
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        trace_id=context.trace_id,
        idempotency_key=idempotency_key,
    )


SettingsDep = Annotated[Settings, Depends(get_settings)]
BootstrapSettingsDep = Annotated[Settings, Depends(get_bootstrap_settings)]
MetadataDep = Annotated[ServiceMetadata, Depends(get_metadata)]
ResourcesDep = Annotated[Resources, Depends(get_resources)]
CacheDep = Annotated[Cache, Depends(get_cache)]
MessagingDep = Annotated[Messaging, Depends(get_messaging)]
DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]
ReadinessDep = Annotated[HealthReport, Depends(get_readiness_report)]
RequestMetadataDep = Annotated[RequestMetadata, Depends(get_request_metadata)]
