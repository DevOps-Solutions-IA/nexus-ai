"""Production application factory (NXS-PLATFORM-002, NXS-API-001).

``create_app`` builds a fully wired :class:`fastapi.FastAPI` instance with no import-time
external connections. Connections are opened by the lifespan; tests build isolated apps
with explicit settings.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from nexus_ai import __version__
from nexus_ai.api.errors import register_exception_handlers
from nexus_ai.api.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from nexus_ai.api.router import api_v1_router
from nexus_ai.api.system import probes_router
from nexus_ai.core.config import Settings, get_settings
from nexus_ai.core.lifecycle import ApplicationLifespan
from nexus_ai.core.telemetry import instrument_app


def _build_middleware(settings: Settings) -> list[Middleware]:
    stack: list[Middleware] = [
        Middleware(SecurityHeadersMiddleware, hsts=settings.is_production),
        Middleware(RequestContextMiddleware, settings=settings),
    ]
    if "*" not in settings.http.allowed_hosts:
        stack.append(
            Middleware(TrustedHostMiddleware, allowed_hosts=list(settings.http.allowed_hosts))
        )
    if settings.http.cors_allow_origins:
        stack.append(
            Middleware(
                CORSMiddleware,
                allow_origins=list(settings.http.cors_allow_origins),
                allow_credentials=settings.http.cors_allow_credentials,
                allow_methods=list(settings.http.cors_allow_methods),
                allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
                max_age=600,
            )
        )
    return stack


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    lifespan_manager = ApplicationLifespan(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await lifespan_manager.startup()
        try:
            yield
        finally:
            await lifespan_manager.shutdown()

    app = FastAPI(
        title=settings.product,
        version=__version__,
        summary="Nexus AI production backend core",
        lifespan=lifespan,
        middleware=_build_middleware(settings),
        docs_url=settings.docs_url,
        redoc_url=None,
        openapi_url=settings.openapi_url,
        swagger_ui_parameters={"defaultModelsExpandDepth": -1},
    )
    app.state.settings = settings
    app.state.lifespan = lifespan_manager

    app.include_router(probes_router)
    app.include_router(api_v1_router)
    register_exception_handlers(app)

    if settings.telemetry.mode != "disabled":
        instrument_app(app)

    return app
