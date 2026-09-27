"""Operations-owned read-only probes, reusing each subsystem's existing authority."""

from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import text

from nexus_ai.cells.service import CellPlacementService
from nexus_ai.core.health import DependencyHealth, HealthStatus
from nexus_ai.infrastructure.messaging import Messaging
from nexus_ai.integrations.executor import GovernedHttpExecutor, OutboundRequest
from nexus_ai.sentinel.database import SentinelDatabase
from nexus_ai.sentinel.errors import SentinelDenied
from nexus_ai.sip_edge.targets import TargetRegistry


class PostgreSQLHealthProbe:
    def __init__(self, database: SentinelDatabase) -> None:
        self._database = database

    async def __call__(self) -> DependencyHealth:
        async with self._database.transaction() as session:
            valid = await session.scalar(text("SELECT 1")) == 1
        return DependencyHealth(
            "sentinel_database", HealthStatus.UP if valid else HealthStatus.DOWN, True
        )


class JetStreamHealthProbe:
    def __init__(self, messaging: Messaging, *, timeout: float = 2) -> None:
        if not 0 < timeout <= 30:
            raise SentinelDenied("probe_timeout_bound")
        self._messaging = messaging
        self._timeout = timeout

    async def __call__(self) -> DependencyHealth:
        return await self._messaging.probe(timeout=self._timeout)


class CellHealthProbe:
    def __init__(self, service: CellPlacementService, *, actor: UUID, cell_id: UUID) -> None:
        self._service = service
        self._actor = actor
        self._cell_id = cell_id

    async def __call__(self) -> DependencyHealth:
        cell = await self._service.inspect_cell(self._actor, self._cell_id)
        return DependencyHealth(
            "cell", HealthStatus.UP if cell.state == "REGISTERED" else HealthStatus.DOWN, True
        )


class PlacementHealthProbe:
    def __init__(
        self, service: CellPlacementService, *, actor: UUID, organization_id: UUID
    ) -> None:
        self._service = service
        self._actor = actor
        self._organization_id = organization_id

    async def __call__(self) -> DependencyHealth:
        placement = await self._service.inspect_placement(self._organization_id, self._actor)
        return DependencyHealth(
            "placement", HealthStatus.UP if placement.state == "ACTIVE" else HealthStatus.DOWN, True
        )


class SipTelephonyHealthProbe:
    def __init__(
        self, registry: TargetRegistry, *, actor: UUID, cell_id: UUID, target_id: UUID
    ) -> None:
        self._registry = registry
        self._actor = actor
        self._cell_id = cell_id
        self._target_id = target_id

    async def __call__(self) -> DependencyHealth:
        target = await self._registry.inspect(self._actor, self._cell_id, self._target_id)
        return DependencyHealth(
            "sip_asterisk_ingress",
            HealthStatus.UP if target.state == "ACTIVE" else HealthStatus.DOWN,
            True,
        )


class AllowlistedHttpHealthProbe:
    def __init__(
        self,
        executor: GovernedHttpExecutor,
        *,
        endpoint_key: str,
        endpoints: dict[str, str],
        timeout: float = 2,
    ) -> None:
        if not 1 <= len(endpoints) <= 32 or not 0 < timeout <= 30:
            raise SentinelDenied("http_health_bounds")
        if executor._settings.max_redirects != 0:
            raise SentinelDenied("health_redirects_must_be_disabled")
        endpoint = endpoints.get(endpoint_key)
        if endpoint is None:
            raise SentinelDenied("http_health_not_allowlisted")
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or len(endpoint) > 1024
        ):
            raise SentinelDenied("unsafe_health_endpoint")
        self._executor = executor
        self._endpoint = endpoint
        self._timeout = timeout

    async def __call__(self) -> DependencyHealth:
        response = await self._executor.send(
            OutboundRequest(
                method="GET",
                url=self._endpoint,
                timeout_seconds=self._timeout,
            )
        )
        if response.final_url != self._endpoint:
            raise SentinelDenied("health_redirect_denied")
        return DependencyHealth(
            "http_health",
            HealthStatus.UP if response.status_code == 200 else HealthStatus.DOWN,
            True,
        )
