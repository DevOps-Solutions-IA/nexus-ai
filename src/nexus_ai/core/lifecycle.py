"""Typed application lifespan and resource orchestration (NXS-HEALTH-001, NXS-RES-001).

Startup loads settings, configures logging and telemetry, constructs dependency managers
and opens connections. A transient dependency outage does NOT crash the process — the
instance stays live and reports ``NOT_READY`` until the dependency recovers. Invalid or
missing mandatory configuration DOES fail startup, and in staging/production a runtime
database role that can bypass tenant RLS fails startup closed (NXS-SEC-003). Shutdown
drains resources in reverse order and is safe to run more than once.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from nexus_ai.api.tenancy import TenantContextResolver, build_resolver
from nexus_ai.core.config import Settings
from nexus_ai.core.errors import ConfigurationError
from nexus_ai.core.health import DependencyHealth, Probe, ReadinessEvaluator
from nexus_ai.core.logging import configure_logging, get_logger
from nexus_ai.core.metadata import ServiceMetadata
from nexus_ai.core.telemetry import configure_telemetry, shutdown_telemetry
from nexus_ai.domain.auth.administration import MembershipService
from nexus_ai.domain.auth.keys import SigningKeyProvider, build_key_provider
from nexus_ai.domain.auth.passwords import build_password_hasher
from nexus_ai.domain.auth.ratelimit import RateLimitGate
from nexus_ai.domain.auth.rbac import AuthorizationService
from nexus_ai.domain.auth.service import AuthService
from nexus_ai.domain.auth.tokens import TokenService
from nexus_ai.domain.organizations.service import OrganizationService
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
    tenant_resolver: TenantContextResolver
    organizations: OrganizationService
    signing_keys: SigningKeyProvider
    token_service: TokenService
    auth: AuthService
    authorizer: AuthorizationService
    memberships: MembershipService


def _bind(adapter: _Probeable, timeout: float) -> Probe:
    async def _run() -> DependencyHealth:
        return await adapter.probe(timeout=timeout)

    return _run


class ApplicationLifespan:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._resources: Resources | None = None
        self._adapters: tuple[Messaging, Cache, Database] | None = None
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

        database = Database(
            settings.database, context_setting=settings.tenancy.context_setting_name
        )
        cache = Cache(settings.cache)
        messaging = Messaging(settings.messaging)
        self._adapters = (messaging, cache, database)

        await self._open("postgresql", database.connect, settings.database.required)
        await self._open("valkey", cache.connect, settings.cache.required)
        await self._open("nats", messaging.connect, settings.messaging.required)

        if settings.database.verify_runtime_role and (
            database.is_connected or settings.is_hardened_environment
        ):
            await self._verify_runtime_role(database)

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
        # Authentication foundation (NXS-AUTH-004/005): signing configuration is
        # fail-closed by the settings validators and by build_key_provider itself, so a
        # startup that reaches this point holds a trusted key configuration.
        signing_keys = build_key_provider(settings.auth)
        token_service = TokenService(
            signing_keys,
            issuer=settings.auth.issuer or settings.service_name,
            audience=settings.auth.audience or settings.product,
            access_token_ttl_seconds=settings.auth.access_token_ttl_seconds,
            clock_skew_seconds=settings.auth.clock_skew_seconds,
        )
        password_hasher = build_password_hasher(settings.auth)
        rate_gate = RateLimitGate(settings.auth, cache)
        authorizer = AuthorizationService(database)
        auth_service = AuthService(
            settings,
            database,
            token_service,
            password_hasher,
            rate_gate,
        )
        self._resources = Resources(
            settings=settings,
            metadata=ServiceMetadata.from_settings(settings),
            database=database,
            cache=cache,
            messaging=messaging,
            readiness=readiness,
            tenant_resolver=build_resolver(settings.tenancy, token_service),
            organizations=OrganizationService(database),
            signing_keys=signing_keys,
            token_service=token_service,
            auth=auth_service,
            authorizer=authorizer,
            memberships=MembershipService(database, authorizer),
        )
        await logger.ainfo(
            "runtime_started",
            environment=str(settings.environment),
            build_sha=settings.build.commit,
        )
        return self._resources

    async def _verify_runtime_role(self, database: Database) -> None:
        """Confirm the runtime database role cannot bypass tenant RLS (NXS-SEC-003).

        In staging/production this is fail-closed: an inability to complete the check is
        itself a security failure, exactly like finding a superuser / BYPASSRLS role.
        Local/test may log a warning and continue. No raw database exception detail is
        put into the raised message or the logs — only a safe error type.
        """
        logger = get_logger("nexus_ai.lifecycle")
        hardened = self._settings.is_hardened_environment
        try:
            report = await database.runtime_role_report()
        except Exception as exc:
            if hardened:
                raise ConfigurationError(
                    "unable to verify the runtime database role cannot bypass tenant RLS; "
                    "refusing to start"
                ) from None
            await logger.awarning("runtime_role_check_skipped", error_type=type(exc).__name__)
            return
        await logger.ainfo("runtime_role", **{k: str(v) for k, v in report.as_payload().items()})
        if report.can_bypass_tenancy:
            if hardened:
                raise ConfigurationError(
                    f"runtime database role {report.role!r} can bypass tenant RLS "
                    "(superuser or BYPASSRLS); refusing to start"
                )
            await logger.awarning(
                "runtime_role_can_bypass_rls",
                role=report.role,
                environment=str(self._settings.environment),
            )

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
        if self._shut_down or self._adapters is None:
            return
        self._shut_down = True
        logger = get_logger("nexus_ai.lifecycle")
        messaging, cache, database = self._adapters
        for name, closer in (
            ("nats", messaging.disconnect),
            ("valkey", cache.disconnect),
            ("postgresql", database.disconnect),
        ):
            try:
                await closer()
            except Exception as exc:
                await logger.awarning("resource_shutdown_error", resource=name, error=str(exc))
        shutdown_telemetry()
        await logger.ainfo("runtime_stopped")
