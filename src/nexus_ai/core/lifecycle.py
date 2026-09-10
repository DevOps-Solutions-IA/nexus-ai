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
from nexus_ai.domain.auth.state import PrincipalStateValidator
from nexus_ai.domain.auth.tokens import TokenService
from nexus_ai.domain.customers import events as customer_events  # noqa: F401 - payload registration
from nexus_ai.domain.customers.service import ConversationService, CustomerService
from nexus_ai.domain.integrations.repository import (
    IntegrationIdempotencyRepository,
    IntegrationSecretStore,
)
from nexus_ai.domain.messaging.repository import MessagingSecretStore
from nexus_ai.domain.organizations.service import OrganizationService
from nexus_ai.domain.provisioning import events as provisioning_events  # noqa: F401
from nexus_ai.domain.provisioning.service import OrganizationProvisioner
from nexus_ai.domain.telephony.repository import TelephonySecretStore
from nexus_ai.domain.tools.repository import ToolIdempotencyRepository
from nexus_ai.events.service import EventPlatform
from nexus_ai.infrastructure.cache import Cache
from nexus_ai.infrastructure.database import Database
from nexus_ai.infrastructure.messaging import Messaging
from nexus_ai.integrations import events as integration_events  # noqa: F401 - payload registration
from nexus_ai.integrations.auth_profiles import AuthProfileApplier
from nexus_ai.integrations.circuit import CircuitBreakerRegistry
from nexus_ai.integrations.credentials import LocalEncryptedVault, VaultClient, build_fernet
from nexus_ai.integrations.destination import DestinationPolicy
from nexus_ai.integrations.executor import GovernedHttpExecutor
from nexus_ai.integrations.ratelimit import OutboundRateLimiter
from nexus_ai.integrations.registry import IntegrationRegistry
from nexus_ai.integrations.service import IntegrationHubService
from nexus_ai.integrations.webhooks import InboundWebhookService
from nexus_ai.messaging import events as messaging_events  # noqa: F401 - payload registration
from nexus_ai.messaging.providers.registry import GovernedMessagingTransport
from nexus_ai.messaging.service import MessagingService
from nexus_ai.messaging.webhooks import InboundMessagingService
from nexus_ai.otp import events as otp_events  # noqa: F401 - payload registration
from nexus_ai.otp.service import OtpService
from nexus_ai.telephony import events as telephony_events  # noqa: F401 - payload registration
from nexus_ai.telephony.providers.registry import GovernedTelephonyTransport
from nexus_ai.telephony.service import TelephonyService
from nexus_ai.telephony.webhooks import InboundTelephonyService
from nexus_ai.tools import events as tool_events  # noqa: F401 - payload registration
from nexus_ai.tools.permissions import ToolPermissionGuard
from nexus_ai.tools.registry import ToolRegistry
from nexus_ai.tools.service import ToolEngine

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
    principal_validator: PrincipalStateValidator
    event_platform: EventPlatform
    provisioner: OrganizationProvisioner
    customers: CustomerService
    conversations: ConversationService
    integration_vault: VaultClient
    integrations: IntegrationRegistry
    integration_hub: IntegrationHubService
    inbound_webhooks: InboundWebhookService
    tools: ToolRegistry
    tool_engine: ToolEngine
    channels: MessagingService
    channel_webhooks: InboundMessagingService
    otp: OtpService
    telephony: TelephonyService
    telephony_webhooks: InboundTelephonyService


def _bind(adapter: _Probeable, timeout: float) -> Probe:
    async def _run() -> DependencyHealth:
        return await adapter.probe(timeout=timeout)

    return _run


class ApplicationLifespan:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._resources: Resources | None = None
        self._adapters: tuple[Messaging, Cache, Database] | None = None
        self._event_platform: EventPlatform | None = None
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

        # Data and event platform (NXS-P04). Startup is fail-closed in hardened
        # environments: if durable JetStream is required and unavailable, or the stream
        # topology cannot be established, ``start`` raises ConfigurationError.
        event_platform = EventPlatform(settings, database, messaging)
        self._event_platform = event_platform
        await event_platform.start()

        probe_timeout = settings.health.probe_timeout_seconds
        probes: list[Probe] = [
            _bind(database, probe_timeout),
            _bind(cache, probe_timeout),
            _bind(messaging, probe_timeout),
            _bind(event_platform, probe_timeout),
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
        principal_validator = PrincipalStateValidator(database)
        auth_service = AuthService(
            settings,
            database,
            token_service,
            password_hasher,
            rate_gate,
            state_validator=principal_validator,
        )
        integrations_settings = settings.integrations
        vault_keys = integrations_settings.vault_key_list()
        if settings.is_hardened_environment and integrations_settings.enabled:
            # Fail startup closed: hardened deployments must ship real key material and
            # must never allow plain-http outbound destinations (NXS-INT-001).
            if not vault_keys:
                raise ConfigurationError(
                    "NXS_INTEGRATIONS__VAULT_ENCRYPTION_KEYS is required outside local/test"
                )
            if not integrations_settings.require_https_outbound:
                raise ConfigurationError(
                    "NXS_INTEGRATIONS__REQUIRE_HTTPS_OUTBOUND must be true outside local/test"
                )
        if not vault_keys and not settings.is_hardened_environment:
            # local/test convenience only.
            from cryptography.fernet import Fernet

            vault_keys = [Fernet.generate_key().decode("ascii")]
            await logger.awarning("integration_vault_ephemeral_key")
        destination_policy = DestinationPolicy(
            require_https=integrations_settings.require_https_outbound
        )
        integration_vault: VaultClient = LocalEncryptedVault(
            IntegrationSecretStore(database), build_fernet(vault_keys)
        )
        http_executor = GovernedHttpExecutor(integrations_settings, destination_policy)
        auth_applier = AuthProfileApplier(integration_vault, destination_policy, http_executor)
        integration_registry = IntegrationRegistry(
            settings, database, event_platform.publisher, integration_vault, destination_policy
        )
        integration_hub = IntegrationHubService(
            settings,
            database,
            event_platform.publisher,
            integration_registry,
            http_executor,
            auth_applier,
            CircuitBreakerRegistry(integrations_settings),
            OutboundRateLimiter(integrations_settings, cache),
            IntegrationIdempotencyRepository(database),
        )
        inbound_webhooks = InboundWebhookService(
            settings, database, event_platform.publisher, integration_vault
        )

        tool_registry = ToolRegistry(
            settings, database, event_platform.publisher, integration_registry
        )
        tool_engine = ToolEngine(
            settings,
            database,
            event_platform.publisher,
            tool_registry,
            integration_hub,
            ToolPermissionGuard(authorizer),
            ToolIdempotencyRepository(database),
        )

        customer_service = CustomerService(settings, database, event_platform.publisher)
        conversation_service = ConversationService(settings, database, event_platform.publisher)
        messaging_vault: VaultClient = LocalEncryptedVault(
            MessagingSecretStore(database), build_fernet(vault_keys)
        )
        channel_service = MessagingService(
            settings,
            database,
            event_platform.publisher,
            messaging_vault,
            GovernedMessagingTransport(http_executor),
            customer_service,
            conversation_service,
        )
        channel_webhooks = InboundMessagingService(
            settings,
            database,
            event_platform.publisher,
            messaging_vault,
            customer_service,
            conversation_service,
            channel_service,
        )
        otp_service = OtpService(
            settings,
            database,
            event_platform.publisher,
            channel_service,
            customer_service,
            conversation_service,
        )
        telephony_vault: VaultClient = LocalEncryptedVault(
            TelephonySecretStore(database), build_fernet(vault_keys)
        )
        telephony_service = TelephonyService(
            settings,
            database,
            event_platform.publisher,
            telephony_vault,
            GovernedTelephonyTransport(
                http_executor,
                timeout_seconds=settings.telephony.provider_timeout_seconds,
            ),
        )
        telephony_webhooks = InboundTelephonyService(
            settings, database, event_platform.publisher, telephony_vault
        )

        self._resources = Resources(
            settings=settings,
            metadata=ServiceMetadata.from_settings(settings),
            database=database,
            cache=cache,
            messaging=messaging,
            readiness=readiness,
            tenant_resolver=build_resolver(settings.tenancy, token_service, principal_validator),
            organizations=OrganizationService(database),
            signing_keys=signing_keys,
            token_service=token_service,
            auth=auth_service,
            authorizer=authorizer,
            memberships=MembershipService(database, authorizer),
            principal_validator=principal_validator,
            event_platform=event_platform,
            provisioner=OrganizationProvisioner(settings, database, event_platform.publisher),
            customers=customer_service,
            conversations=conversation_service,
            integration_vault=integration_vault,
            integrations=integration_registry,
            integration_hub=integration_hub,
            inbound_webhooks=inbound_webhooks,
            tools=tool_registry,
            tool_engine=tool_engine,
            channels=channel_service,
            channel_webhooks=channel_webhooks,
            otp=otp_service,
            telephony=telephony_service,
            telephony_webhooks=telephony_webhooks,
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
        if self._event_platform is not None:
            try:
                await self._event_platform.stop()
            except Exception as exc:
                await logger.awarning("event_platform_shutdown_error", error=str(exc))
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
