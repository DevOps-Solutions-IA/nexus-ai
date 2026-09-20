"""Explicit internal-only application and P11 issuer configuration."""

from __future__ import annotations

import asyncio
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import Field, SecretStr, ValidationError, model_validator

from nexus_ai.core.config import DatabaseSettings, Settings
from nexus_ai.core.errors import ConfigurationError
from nexus_ai.core.health import HealthStatus
from nexus_ai.infrastructure.database import Database
from nexus_ai.sip_edge.api import RouteHandles, create_resolver_app
from nexus_ai.sip_edge.authentication import EdgeAuthenticator
from nexus_ai.sip_edge.contracts import StrictContract
from nexus_ai.sip_edge.egress import EgressPermits
from nexus_ai.sip_edge.inbound import InboundRoutes
from nexus_ai.sip_edge.locator import DidLocator
from nexus_ai.sip_edge.peers import PeerPolicy, PeerProfile
from nexus_ai.sip_edge.security import EdgeCredential


class EdgeSecret(StrictContract):
    edge_id: UUID
    hmac_secret: SecretStr


class ResolverSecrets(StrictContract):
    edges: Annotated[tuple[EdgeSecret, ...], Field(min_length=1, max_length=128)]
    peers: Annotated[tuple[PeerProfile, ...], Field(min_length=1, max_length=256)]
    permit_key: SecretStr
    handle_key: SecretStr

    @model_validator(mode="after")
    def validate_material(self) -> ResolverSecrets:
        PeerPolicy(self.peers)
        if len({item.edge_id for item in self.edges}) != len(self.edges):
            raise ValueError("duplicate edge identity")
        for item in self.edges:
            EdgeCredential(item.edge_id, bytes.fromhex(item.hmac_secret.get_secret_value()))
        Fernet(self.permit_key.get_secret_value().encode("ascii"))
        Fernet(self.handle_key.get_secret_value().encode("ascii"))
        return self


def load_secrets(settings: Settings) -> ResolverSecrets:
    if not settings.sip_edge.enabled or settings.sip_edge.secret_file is None:
        raise ConfigurationError("SIP edge is not explicitly configured.")
    try:
        path = Path(settings.sip_edge.secret_file)
        metadata = path.stat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o027
            or metadata.st_size > 65536
        ):
            raise ValueError("unsafe secret configuration")
        with path.open("rb") as stream:
            content = stream.read(65537)
        if len(content) > 65536:
            raise ValueError("unbounded secret configuration")
        return ResolverSecrets.model_validate_json(content)
    except OSError, ValueError, ValidationError:
        raise ConfigurationError("SIP edge configuration is invalid.") from None


def build_issuer(settings: Settings, database: Database) -> EgressPermits:
    secrets = load_secrets(settings)
    return EgressPermits(
        database, secrets.permit_key.get_secret_value().encode("ascii"), PeerPolicy(secrets.peers)
    )


def create_application() -> FastAPI:
    settings = Settings()
    secrets = load_secrets(settings)
    if settings.sip_edge.locator_dsn is None:
        raise ConfigurationError("SIP locator configuration is required.")
    database = Database(settings.database)
    discovery = Database(DatabaseSettings(dsn=settings.sip_edge.locator_dsn))
    peers = PeerPolicy(secrets.peers)
    app = create_resolver_app(
        EdgeAuthenticator(
            database,
            tuple(
                EdgeCredential(item.edge_id, bytes.fromhex(item.hmac_secret.get_secret_value()))
                for item in secrets.edges
            ),
        ),
        peers,
        InboundRoutes(database, DidLocator(discovery)),
        RouteHandles(secrets.handle_key.get_secret_value().encode("ascii")),
        EgressPermits(database, secrets.permit_key.get_secret_value().encode("ascii"), peers),
    )
    app.state.ready = False

    async def liveness() -> JSONResponse:
        return JSONResponse({"status": "ALIVE"})

    async def readiness() -> JSONResponse:
        ready = False
        if app.state.ready:
            reports = await asyncio.gather(
                database.probe(timeout=0.5), discovery.probe(timeout=0.5)
            )
            ready = all(report.status is HealthStatus.UP for report in reports)
        return JSONResponse(
            {"status": "READY" if ready else "NOT_READY"}, status_code=200 if ready else 503
        )

    app.add_api_route("/health/live", liveness, methods=["GET"])
    app.add_api_route("/health/ready", readiness, methods=["GET"])

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        try:
            await database.connect()
            await discovery.connect()
            runtime_role = await database.runtime_role_report()
            locator_role = await discovery.runtime_role_report()
            if (
                runtime_role.role != "nexus_runtime"
                or runtime_role.can_bypass_tenancy
                or locator_role.role != "nexus_sip_locator"
                or locator_role.can_bypass_tenancy
            ):
                raise ConfigurationError("SIP database roles are unsafe.")
            app.state.ready = True
            yield
        finally:
            app.state.ready = False
            await discovery.disconnect()
            await database.disconnect()

    app.router.lifespan_context = lifespan
    return app
