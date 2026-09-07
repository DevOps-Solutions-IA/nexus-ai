"""Typed application lifespan and resource orchestration (NXS-HEALTH-001, NXS-RES-001).

Startup loads settings, configures logging and telemetry, constructs dependency managers
and opens connections. A transient dependency outage does NOT crash the process — the
instance stays live and reports ``NOT_READY`` until the dependency recovers. Invalid or
missing mandatory configuration DOES fail startup. Shutdown drains resources in reverse
order and is safe to run more than once.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from nexus_ai.core.config import Settings
from nexus_ai.core.errors import ConfigurationError
from nexus_ai.core.health import DependencyHealth, Probe, ReadinessEvaluator
from nexus_ai.core.logging import configure_logging, get_logger
from nexus_ai.core.metadata import ServiceMetadata
from nexus_ai.core.telemetry import configure_telemetry, shutdown_telemetry
from nexus_ai.infrastructure.cache import Cache
from nexus_ai.infrastructure.database import Database
from nexus_ai.infrastructure.messaging import Messaging

_Connector = Callable[[], Awaitable[None]]


class _Probeable(Protocol):
    async def probe(self, *, timeout: float) -> DependencyHealth: ...


@dataclass(slots=True)
class Resources:
    settings: Settings
    metadata: ServiceMetadata
    database: Database
    cache: Cache
    messaging: Messaging
    readiness: ReadinessEvaluator


def _bind(adapter: _Probeable, timeout: float) -> Probe:
    async def _run() -> DependencyHealth:
        return await adapter.probe(timeout=timeout)

    return _run


class ApplicationLifespan:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._resources: Resources | None = None
        self._shut_down = False

    @property
    def resources(self) -> Resources:
        if self._resources is None:
            raise ConfigurationError("application resources are not initialised")
        return self._resources

    async def startup(self) -> Resources:
        settings = self._settings
        configure_logging(settings)
        configure_telemetry(settings)
        logger = get_logger("nexus_ai.lifecycle")

        database = Database(settings.database)
        cache = Cache(settings.cache)
        messaging = Messaging(settings.messaging)

        await self._open("postgresql", database.connect, settings.database.required)
        await self._open("valkey", cache.connect, settings.cache.required)
        await self._open("nats", messaging.connect, settings.messaging.required)

        probe_timeout = settings.health.probe_timeout_seconds
        probes: list[Probe] = [
            _bind(database, probe_timeout),
            _bind(cache, probe_timeout),
            _bind(messaging, probe_timeout),
        ]
        readiness = ReadinessEvaluator(
            probes,
            ttl_seconds=settings.health.cache_ttl_seconds,
            probe_timeout=probe_timeout,
        )
        self._resources = Resources(
            settings=settings,
            metadata=ServiceMetadata.from_settings(settings),
            database=database,
            cache=cache,
            messaging=messaging,
            readiness=readiness,
        )
        await logger.ainfo(
            "runtime_started",
            environment=str(settings.environment),
            build_sha=settings.build.commit,
        )
        return self._resources

    async def _open(self, name: str, connect: _Connector, required: bool) -> None:
        logger = get_logger("nexus_ai.lifecycle")
        try:
            await connect()
        except ConfigurationError:
            if required:
                raise
        except Exception as exc:
            event = "dependency_unavailable_at_startup" if required else "optional_dependency_down"
            await logger.awarning(event, resource=name, error_code=type(exc).__name__)

    async def shutdown(self) -> None:
        if self._shut_down or self._resources is None:
            return
        self._shut_down = True
        logger = get_logger("nexus_ai.lifecycle")
        for name, closer in (
            ("nats", self._resources.messaging.disconnect),
            ("valkey", self._resources.cache.disconnect),
            ("postgresql", self._resources.database.disconnect),
        ):
            try:
                await closer()
            except Exception as exc:
                await logger.awarning("resource_shutdown_error", resource=name, error=str(exc))
        shutdown_telemetry()
        await logger.ainfo("runtime_stopped")
