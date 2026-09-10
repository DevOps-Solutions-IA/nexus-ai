"""Strongly validated, environment-aware runtime configuration (NXS-CONFIG-001).

All configuration is read from the process environment with the ``NXS_`` prefix and a
``__`` nested delimiter, for example ``NXS_DATABASE__POOL_SIZE=10``. Secrets are held in
``SecretStr`` fields and never appear in ``repr``/logs. Production fails fast when a
required security-sensitive value is missing or unsafe.
"""

from __future__ import annotations

import enum
from functools import lru_cache
from typing import Literal, Self
from urllib.parse import urlsplit, urlunsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    computed_field,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(enum.StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


def redact_url(raw: str) -> str:
    """Return ``raw`` with any userinfo password replaced by ``***``."""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return "***"
    if parts.password is None and parts.username is None:
        return raw
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    user = parts.username or ""
    netloc = f"{user}:***@{host}" if user else f":***@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _require_scheme(raw: str, allowed: tuple[str, ...], label: str) -> str:
    parts = urlsplit(raw)
    if parts.scheme not in allowed:
        raise ValueError(f"{label} must use one of {allowed}, got {parts.scheme!r}")
    if not parts.hostname:
        raise ValueError(f"{label} must include a host")
    return raw


class HttpSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    host: str = "0.0.0.0"  # noqa: S104 - bind address is deployment-controlled
    port: int = Field(default=8080, ge=1, le=65535)
    allowed_hosts: tuple[str, ...] = ("*",)
    cors_allow_origins: tuple[str, ...] = ()
    cors_allow_credentials: bool = False
    cors_allow_methods: tuple[str, ...] = ("GET", "POST", "PATCH", "DELETE", "OPTIONS")
    docs_enabled: bool = True
    request_id_header: str = "X-Request-ID"
    correlation_id_header: str = "X-Correlation-ID"
    max_request_header_bytes: int = Field(default=16384, ge=1024, le=1_048_576)
    trust_forwarded_headers: bool = False


class DatabaseSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    dsn: SecretStr | None = None
    migration_dsn: SecretStr | None = None
    runtime_role: str = Field(default="nexus_runtime", min_length=1, max_length=63)
    verify_runtime_role: bool = True
    pool_size: int = Field(default=5, ge=1, le=100)
    max_overflow: int = Field(default=5, ge=0, le=100)
    pool_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    pool_recycle_seconds: int = Field(default=1800, ge=60, le=86400)
    connect_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    command_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    required: bool = True

    @field_validator("dsn", "migration_dsn")
    @classmethod
    def _validate_dsn(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        _require_scheme(
            value.get_secret_value(),
            ("postgresql+asyncpg", "postgresql"),
            "NXS_DATABASE__DSN",
        )
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def safe_dsn(self) -> str | None:
        if self.dsn is None:
            return None
        raw = self.dsn.get_secret_value()
        if raw.startswith("postgresql://"):
            raw = raw.replace("postgresql://", "postgresql+asyncpg://", 1)
        return redact_url(raw)

    @staticmethod
    def _as_async(raw: str) -> str:
        if raw.startswith("postgresql://"):
            return raw.replace("postgresql://", "postgresql+asyncpg://", 1)
        return raw

    def async_dsn(self) -> str:
        if self.dsn is None:
            raise ValueError("database DSN is not configured")
        return self._as_async(self.dsn.get_secret_value())

    def migration_async_dsn(self) -> str:
        source = self.migration_dsn or self.dsn
        if source is None:
            raise ValueError("no database DSN is configured for migrations")
        return self._as_async(source.get_secret_value())

    @computed_field  # type: ignore[prop-decorator]
    @property
    def safe_migration_dsn(self) -> str | None:
        if self.migration_dsn is None:
            return None
        return redact_url(self._as_async(self.migration_dsn.get_secret_value()))


class CacheSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: SecretStr | None = None
    connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    operation_timeout_seconds: float = Field(default=3.0, gt=0, le=60)
    pool_max_connections: int = Field(default=10, ge=1, le=200)
    required: bool = True

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        _require_scheme(
            value.get_secret_value(), ("redis", "rediss", "valkey", "valkeys"), "NXS_CACHE__URL"
        )
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def safe_url(self) -> str | None:
        return None if self.url is None else redact_url(self.url.get_secret_value())

    def client_url(self) -> str:
        if self.url is None:
            raise ValueError("cache URL is not configured")
        raw = self.url.get_secret_value()
        return raw.replace("valkey://", "redis://", 1).replace("valkeys://", "rediss://", 1)


class MessagingSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: SecretStr | None = None
    connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    reconnect_time_wait_seconds: float = Field(default=2.0, gt=0, le=60)
    max_reconnect_attempts: int = Field(default=60, ge=-1, le=1000)
    drain_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    required: bool = True

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        _require_scheme(value.get_secret_value(), ("nats", "tls"), "NXS_MESSAGING__URL")
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def safe_url(self) -> str | None:
        return None if self.url is None else redact_url(self.url.get_secret_value())

    def servers(self) -> list[str]:
        if self.url is None:
            raise ValueError("messaging URL is not configured")
        return [item.strip() for item in self.url.get_secret_value().split(",") if item.strip()]


class EventsSettings(BaseModel):
    """Data and event platform configuration (NXS-EVENT-002..009, NXS-DATA-002).

    Nexus AI guarantees at-least-once event transport. Exactly-once transport is not
    claimed — duplicate-safe business effects come from idempotent consumers. Every
    bound below is explicit and validated; hardened environments additionally require
    durable JetStream and refuse an unsafe polling cadence.
    """

    model_config = ConfigDict(frozen=True)

    subject_prefix: str = Field(default="nxs", pattern=r"^[a-z][a-z0-9]{1,15}$")
    stream_name: str = Field(default="NXS_EVENTS", pattern=r"^[A-Z][A-Z0-9_]{2,31}$")
    dead_letter_stream_name: str = Field(
        default="NXS_EVENTS_DLQ", pattern=r"^[A-Z][A-Z0-9_]{2,31}$"
    )
    stream_max_age_seconds: int = Field(default=1_209_600, ge=3600, le=31_536_000)
    stream_replicas: int = Field(default=1, ge=1, le=5)

    require_jetstream: bool = True
    bootstrap_topology: bool = True

    publisher_enabled: bool = True
    publisher_poll_interval_seconds: float = Field(default=1.0, ge=0.05, le=60)
    publisher_batch_size: int = Field(default=100, ge=1, le=1000)
    publisher_lease_seconds: int = Field(default=30, ge=5, le=600)
    publish_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    max_publish_attempts: int = Field(default=8, ge=1, le=50)

    consumers_enabled: bool = False
    consumer_batch_size: int = Field(default=16, ge=1, le=256)
    consumer_concurrency: int = Field(default=8, ge=1, le=64)
    consumer_ack_wait_seconds: int = Field(default=30, ge=1, le=600)
    consumer_poll_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    handler_timeout_seconds: float = Field(default=20.0, gt=0, le=300)
    max_delivery_attempts: int = Field(default=8, ge=1, le=50)

    retry_base_delay_seconds: float = Field(default=1.0, gt=0, le=60)
    retry_max_delay_seconds: float = Field(default=300.0, gt=0, le=3600)
    dedupe_window_seconds: int = Field(default=120, ge=1, le=86_400)

    @model_validator(mode="after")
    def _bounds(self) -> Self:
        if self.retry_base_delay_seconds > self.retry_max_delay_seconds:
            raise ValueError("retry_base_delay_seconds must not exceed retry_max_delay_seconds")
        if self.publisher_batch_size < 1:
            raise ValueError("publisher_batch_size must be positive")
        return self

    def environment_segment(self, environment: Environment) -> str:
        return environment.value

    def tenant_subject_filter(self, environment: Environment) -> str:
        return f"{self.subject_prefix}.{environment.value}.tenant.>"

    def global_subject_filter(self, environment: Environment) -> str:
        return f"{self.subject_prefix}.{environment.value}.global.>"

    def main_stream_subjects(self, environment: Environment) -> list[str]:
        return [
            self.tenant_subject_filter(environment),
            self.global_subject_filter(environment),
        ]

    def dead_letter_subject_filter(self, environment: Environment) -> str:
        return f"{self.subject_prefix}.{environment.value}.dlq.>"


class IntegrationsSettings(BaseModel):
    """Integration Hub configuration (NXS-INT-001).

    Every bound is explicit and validated. The governed HTTP executor is deny-by-default:
    SSRF destination policy is always on, redirects are disabled unless a bounded count is
    configured, and hardened environments additionally require https for every outbound
    destination and refuse an ephemeral vault key.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = True

    # -- credential vault seam --
    vault_encryption_keys: str = ""
    allow_ephemeral_vault_key: bool = False

    # -- governed HTTP executor bounds --
    connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    read_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    total_timeout_seconds: float = Field(default=25.0, gt=0, le=180)
    max_request_bytes: int = Field(default=1_048_576, ge=1024, le=16_777_216)
    max_response_bytes: int = Field(default=2_097_152, ge=1024, le=33_554_432)
    max_response_header_bytes: int = Field(default=65_536, ge=1024, le=1_048_576)
    max_redirects: int = Field(default=0, ge=0, le=5)
    user_agent: str = Field(default="NexusAI-IntegrationHub/1.0", min_length=1, max_length=128)
    require_https_outbound: bool = False
    #: Header NAMES (lowercased) whose values are redacted everywhere, on top of the
    #: always-redacted Authorization / api-key / cookie set.
    sensitive_header_names: str = ""

    # -- retry policy --
    retry_max_attempts: int = Field(default=3, ge=1, le=8)
    retry_base_delay_seconds: float = Field(default=0.2, gt=0, le=30)
    retry_max_delay_seconds: float = Field(default=10.0, gt=0, le=120)
    retry_max_elapsed_seconds: float = Field(default=30.0, gt=0, le=300)

    # -- circuit breaker --
    circuit_failure_threshold: int = Field(default=5, ge=1, le=100)
    circuit_reset_seconds: float = Field(default=30.0, gt=0, le=3600)
    circuit_half_open_max_calls: int = Field(default=1, ge=1, le=20)

    # -- outbound rate limiting --
    outbound_rate_limit_per_minute: int = Field(default=600, ge=1, le=100_000)
    outbound_rate_limit_burst: int = Field(default=60, ge=1, le=10_000)

    # -- idempotency --
    idempotency_retention_seconds: int = Field(default=86_400, ge=60, le=2_592_000)

    # -- OpenAPI ingestion bounds --
    openapi_max_document_bytes: int = Field(default=2_097_152, ge=1024, le=16_777_216)
    openapi_max_operations: int = Field(default=100, ge=1, le=2000)
    openapi_max_depth: int = Field(default=40, ge=4, le=200)

    # -- inbound webhooks --
    webhook_max_body_bytes: int = Field(default=1_048_576, ge=256, le=16_777_216)
    webhook_default_tolerance_seconds: int = Field(default=300, ge=30, le=3600)
    webhook_receipt_retention_seconds: int = Field(default=604_800, ge=3600, le=2_592_000)

    @model_validator(mode="after")
    def _bounds(self) -> Self:
        if self.read_timeout_seconds > self.total_timeout_seconds:
            raise ValueError("read_timeout_seconds must not exceed total_timeout_seconds")
        if self.connect_timeout_seconds > self.total_timeout_seconds:
            raise ValueError("connect_timeout_seconds must not exceed total_timeout_seconds")
        if self.retry_base_delay_seconds > self.retry_max_delay_seconds:
            raise ValueError("retry_base_delay_seconds must not exceed retry_max_delay_seconds")
        if self.max_request_bytes > self.max_response_bytes * 8:
            raise ValueError("max_request_bytes is disproportionately large")
        if self.allow_ephemeral_vault_key and self.vault_encryption_keys:
            raise ValueError(
                "NXS_INTEGRATIONS__ALLOW_EPHEMERAL_VAULT_KEY cannot be combined with keys"
            )
        return self

    def vault_key_list(self) -> list[str]:
        return [k.strip() for k in self.vault_encryption_keys.split(",") if k.strip()]

    def sensitive_headers(self) -> frozenset[str]:
        raw = self.sensitive_header_names.split(",")
        return frozenset(h.strip().lower() for h in raw if h.strip())


class ToolsSettings(BaseModel):
    """Tool Engine configuration (NXS-TOOL-001).

    The Tool Engine never opens a network connection itself — every external call routes
    through the Integration Hub — so its bounds are about the registry, the invocation
    policy pipeline and durable idempotency.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    #: Per-organization ceiling on a tool's declared risk class. A tool that declares a
    #: higher class than the ceiling cannot be ACTIVE. Bounded — there is no autonomous
    #: approval flow.
    max_risk_class: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "HIGH"
    max_input_schema_bytes: int = Field(default=65_536, ge=256, le=1_048_576)
    max_arguments_bytes: int = Field(default=262_144, ge=256, le=4_194_304)
    idempotency_retention_seconds: int = Field(default=86_400, ge=60, le=2_592_000)
    default_timeout_seconds: float = Field(default=30.0, gt=0, le=120)


class ChannelsSettings(BaseModel):
    """Messaging Channels configuration (NXS-P09: WhatsApp / Email / SMS).

    Channel adapters never open their own socket — every outbound provider call routes
    through the NXS-P07 governed HTTP executor — so these bounds are about inbound
    webhook size, durable outbound idempotency and delivery-callback tolerance.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    max_webhook_body_bytes: int = Field(default=1_048_576, ge=1_024, le=10_485_760)
    send_idempotency_retention_seconds: int = Field(default=86_400, ge=60, le=2_592_000)
    provider_timeout_seconds: float = Field(default=20.0, gt=0, le=60)
    #: Tolerance window for a signed generic (Email / SMS) inbound webhook whose
    #: X-Messaging-Timestamp is part of the HMAC. A correctly-signed request whose
    #: timestamp is outside +/- this window is rejected as a replay. WhatsApp's Meta
    #: signature has no timestamp protocol and is unaffected.
    webhook_timestamp_tolerance_seconds: int = Field(default=300, ge=30, le=3_600)
    #: Bounds for a channel account's free-form ``configuration`` object.
    max_account_config_keys: int = Field(default=12, ge=1, le=64)
    max_account_config_key_length: int = Field(default=64, ge=8, le=256)
    max_account_config_value_length: int = Field(default=512, ge=16, le=8_192)
    max_account_config_depth: int = Field(default=3, ge=1, le=8)
    max_account_config_bytes: int = Field(default=4_096, ge=256, le=65_536)


class OtpSettings(BaseModel):
    """OTP Services configuration (NXS-P10: NXS-OTP-001).

    The OTP subsystem never opens its own socket — delivery routes through the NXS-P09
    messaging service — so these bounds are about code generation, the keyed verifier,
    challenge lifetime and durable issuance / verification throttling.

    ``pepper`` is secret key material: it never appears in ``repr``/logs, is never
    persisted with a challenge, and a hardened environment fails fast when it is absent
    (see ``Settings._hardened_environment_safety``).
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    #: Numeric code length. Bounded well away from a brute-forceable keyspace.
    code_length: int = Field(default=6, ge=6, le=10)
    ttl_seconds: int = Field(default=300, ge=60, le=1_800)
    max_attempts: int = Field(default=5, ge=1, le=10)
    #: Minimum interval between two successful issuances for the same
    #: (organization, destination, purpose) — a resend before this is rejected.
    resend_cooldown_seconds: int = Field(default=60, ge=15, le=900)
    #: Durable issuance burst ceiling per (organization, destination, purpose) window.
    max_issues_per_window: int = Field(default=5, ge=1, le=50)
    issue_window_seconds: int = Field(default=3_600, ge=60, le=86_400)
    #: HMAC pepper. A 64-character hex string (>= 32 bytes of entropy).
    pepper: SecretStr | None = None
    #: Explicit opt-in to an ephemeral per-process pepper for local/test only.
    allow_ephemeral_pepper: bool = False

    @field_validator("pepper")
    @classmethod
    def _pepper(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value().strip()
        if len(raw) < 32:
            raise ValueError("NXS_OTP__PEPPER must be at least 32 characters of secret entropy")
        return SecretStr(raw)

    @model_validator(mode="after")
    def _pepper_not_both(self) -> Self:
        if self.allow_ephemeral_pepper and self.pepper is not None:
            raise ValueError(
                "NXS_OTP__ALLOW_EPHEMERAL_PEPPER cannot be combined with a configured "
                "NXS_OTP__PEPPER"
            )
        return self


class TelephonySettings(BaseModel):
    """Telephony Foundation configuration (NXS-P11: NXS-TEL-001).

    The telephony subsystem never opens a raw socket or runs a shell — every provider
    call routes through the NXS-P07 governed HTTP executor (ARI / provider REST) — so
    these bounds are about provider-call timeouts, inbound webhook size / freshness and
    the bounded account configuration surface. No SIP password, ARI credential or
    provider token is ever configured here: those live in the encrypted vault.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    #: Hard upper bound on any single provider control operation (create call, hangup,
    #: send DTMF). The stricter of this and the P07 executor timeout wins.
    provider_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    #: Inbound provider webhook body ceiling.
    max_webhook_body_bytes: int = Field(default=262_144, ge=1_024, le=4_194_304)
    #: Freshness window for a signed inbound provider webhook whose timestamp is part of
    #: the HMAC. A correctly-signed callback outside +/- this window is a replay.
    webhook_timestamp_tolerance_seconds: int = Field(default=300, ge=30, le=3_600)
    #: Durable retention for the provider-event idempotency log (seconds).
    event_idempotency_retention_seconds: int = Field(default=604_800, ge=3_600, le=2_592_000)
    #: Durable retention for an outbound-call idempotency claim (seconds).
    outbound_idempotency_retention_seconds: int = Field(default=86_400, ge=60, le=2_592_000)
    #: Maximum DTMF sequence length accepted in one request.
    max_dtmf_sequence_length: int = Field(default=32, ge=1, le=128)
    #: Bounds for a telephony account's free-form ``configuration`` object.
    max_account_config_keys: int = Field(default=12, ge=1, le=64)
    max_account_config_key_length: int = Field(default=64, ge=8, le=256)
    max_account_config_value_length: int = Field(default=512, ge=16, le=8_192)
    max_account_config_bytes: int = Field(default=4_096, ge=256, le=65_536)
    #: The default country (E.164 calling code digits) used to canonicalize a national
    #: destination number when an account does not override it.
    default_country: str = Field(default="1", pattern=r"^[1-9][0-9]{0,2}$")


class VoiceSettings(BaseModel):
    """ElevenLabs Voice configuration (NXS-P12: NXS-VOICE-001 / NXS-EL-001).

    The voice subsystem connects a real-time provider (ElevenLabs) to an ACTIVE NXS-P11
    media session. It never owns call state, tenancy or routing. Every provider REST call
    routes through the NXS-P07 governed HTTP executor and every WebSocket through the
    bounded :class:`~nexus_ai.voice.transport.VoiceStreamTransport`. No provider API key
    is configured here — it lives in the encrypted vault. These bounds are about
    real-time transport safety (frame / queue / message ceilings and every timeout).
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    #: Hard ceiling on a provider control REST call (signed-URL fetch, session create).
    provider_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    #: WebSocket open / handshake ceiling.
    connect_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    #: No provider frame for this long ends the session (idle guard).
    idle_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    #: Absolute lifetime ceiling for a single real-time session.
    max_session_seconds: float = Field(default=3_600.0, gt=0, le=14_400)
    #: Largest single inbound provider WebSocket message.
    max_message_bytes: int = Field(default=131_072, ge=1_024, le=1_048_576)
    #: Largest single decoded audio frame handed to / from the media side.
    max_audio_frame_bytes: int = Field(default=32_768, ge=256, le=262_144)
    #: Bounded depth of the inbound and the outbound audio queues (backpressure).
    audio_queue_depth: int = Field(default=64, ge=4, le=1_024)
    #: Inbound provider webhook (ElevenLabs post-call) body ceiling.
    max_webhook_body_bytes: int = Field(default=1_048_576, ge=1_024, le=8_388_608)
    #: Freshness window for a signed provider webhook whose timestamp is HMAC-bound.
    webhook_timestamp_tolerance_seconds: int = Field(default=300, ge=30, le=3_600)
    #: Durable retention for the provider-event idempotency log (seconds).
    event_idempotency_retention_seconds: int = Field(default=604_800, ge=3_600, le=2_592_000)
    #: Bounds for a voice provider account's free-form ``configuration`` object.
    max_account_config_keys: int = Field(default=12, ge=1, le=64)
    max_account_config_key_length: int = Field(default=64, ge=8, le=256)
    max_account_config_value_length: int = Field(default=1_024, ge=16, le=8_192)
    max_account_config_bytes: int = Field(default=8_192, ge=256, le=65_536)
    #: The ElevenLabs REST API origin. A bare https origin — never a per-call URL surface.
    elevenlabs_api_base: str = Field(
        default="https://api.elevenlabs.io", pattern=r"^https://[a-z0-9.-]+(?::[0-9]{1,5})?$"
    )


class LoggingSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    format: Literal["json", "console"] = "json"
    max_field_length: int = Field(default=8192, ge=256, le=65536)


class TelemetrySettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    mode: Literal["disabled", "local", "export"] = "disabled"
    otlp_endpoint: str | None = None
    sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    service_namespace: str = "nexus-ai"

    @model_validator(mode="after")
    def _export_requires_endpoint(self) -> Self:
        if self.mode == "export" and not self.otlp_endpoint:
            raise ValueError("telemetry export mode requires NXS_TELEMETRY__OTLP_ENDPOINT")
        return self


class HealthSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    probe_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    cache_ttl_seconds: float = Field(default=1.0, ge=0.0, le=60)


class BuildMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    commit: str = "unknown"
    built_at: str | None = None


class TenancySettings(BaseModel):
    """Multi-tenant boundary configuration (NXS-TENANT-002, NXS-SEC-003).

    ``header_resolver_enabled`` allows a request header to establish tenant scope. It is
    a TEST/LOCAL convenience only — production configuration rejects it. Authenticated
    identity resolves tenant scope through the P03 bearer-token resolver.
    """

    model_config = ConfigDict(frozen=True)

    header_resolver_enabled: bool = False
    context_header: str = "X-NXS-Organization-ID"
    require_active_organization: bool = True
    context_setting_name: str = Field(default="nxs.organization_id", pattern=r"^[a-z_]+\.[a-z_]+$")


class AuthSettings(BaseModel):
    """Authentication foundation configuration (NXS-AUTH-001..009).

    Signing key material is a secret: it never appears in ``repr`` or logs. Key
    configuration is fail-closed — no signing key and no explicit ephemeral flag is a
    startup error in every environment, and staging/production additionally reject
    ephemeral development keys, insecure Argon2 work factors and local-only rate
    limiting.
    """

    model_config = ConfigDict(frozen=True)

    issuer: str | None = None
    audience: str | None = None
    access_token_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    refresh_token_ttl_seconds: int = Field(default=1_209_600, ge=3600, le=2_592_000)
    clock_skew_seconds: int = Field(default=30, ge=0, le=120)
    allowed_algorithms: tuple[str, ...] = ("EdDSA",)

    signing_key: SecretStr | None = None
    signing_key_file: str | None = None
    verification_key_seeds: str = ""
    allow_ephemeral_signing_key: bool = False

    argon2_time_cost: int = Field(default=3, ge=1, le=32)
    argon2_memory_cost: int = Field(default=65_536, ge=8_192, le=1_048_576)
    argon2_parallelism: int = Field(default=1, ge=1, le=16)
    min_password_length: int = Field(default=12, ge=8, le=256)
    max_password_length: int = Field(default=128, ge=16, le=1024)

    login_max_failures: int = Field(default=5, ge=1, le=100)
    login_failure_window_seconds: int = Field(default=300, ge=10, le=86_400)
    rate_limit_backend: Literal["auto", "cache", "local"] = "auto"

    @field_validator("allowed_algorithms")
    @classmethod
    def _algorithms(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != ("EdDSA",):
            raise ValueError("only the EdDSA algorithm is supported (NXS_AUTH__ALLOWED_ALGORITHMS)")
        return value

    @field_validator("signing_key")
    @classmethod
    def _signing_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value().strip()
        if len(raw) != 64 or any(c not in "0123456789abcdefABCDEF" for c in raw):
            raise ValueError("NXS_AUTH__SIGNING_KEY must be a 64-character hex Ed25519 seed")
        return SecretStr(raw.lower())

    @model_validator(mode="after")
    def _password_policy_bounds(self) -> Self:
        if self.min_password_length > self.max_password_length:
            raise ValueError("min_password_length must not exceed max_password_length")
        return self

    @model_validator(mode="after")
    def _ephemeral_key_environment(self) -> Self:
        # Validated against Settings.environment in the outer hardened validator; here we
        # only make sure the flag is not combined with real key material.
        if self.allow_ephemeral_signing_key and (
            self.signing_key is not None or self.signing_key_file is not None
        ):
            raise ValueError(
                "NXS_AUTH__ALLOW_EPHEMERAL_SIGNING_KEY cannot be combined "
                "with configured key material"
            )
        return self

    def verification_seed_list(self) -> list[str]:
        return [item.strip() for item in self.verification_key_seeds.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NXS_",
        env_nested_delimiter="__",
        extra="ignore",
        frozen=True,
        case_sensitive=False,
    )

    environment: Environment = Environment.LOCAL
    product: str = "Nexus AI"
    service_name: str = "nexus-ai-backend"
    api_version: str = "v1"
    startup_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    shutdown_timeout_seconds: float = Field(default=30.0, gt=0, le=300)

    http: HttpSettings = Field(default_factory=HttpSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    messaging: MessagingSettings = Field(default_factory=MessagingSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    health: HealthSettings = Field(default_factory=HealthSettings)
    tenancy: TenancySettings = Field(default_factory=TenancySettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    events: EventsSettings = Field(default_factory=EventsSettings)
    integrations: IntegrationsSettings = Field(default_factory=IntegrationsSettings)
    tools: ToolsSettings = Field(default_factory=ToolsSettings)
    channels: ChannelsSettings = Field(default_factory=ChannelsSettings)
    otp: OtpSettings = Field(default_factory=OtpSettings)
    telephony: TelephonySettings = Field(default_factory=TelephonySettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    build: BuildMetadata = Field(default_factory=BuildMetadata)

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @property
    def docs_url(self) -> str | None:
        return "/docs" if self.http.docs_enabled else None

    @property
    def openapi_url(self) -> str | None:
        return "/openapi.json" if self.http.docs_enabled else None

    @property
    def is_hardened_environment(self) -> bool:
        """staging and production share the fail-closed security rules."""
        return self.environment in {Environment.STAGING, Environment.PRODUCTION}

    @model_validator(mode="after")
    def _hardened_environment_safety(self) -> Self:
        if not self.is_hardened_environment:
            return self
        problems: list[str] = []
        if self.tenancy.header_resolver_enabled:
            problems.append("NXS_TENANCY__HEADER_RESOLVER_ENABLED must be false outside local/test")
        if self.database.required and not self.database.verify_runtime_role:
            problems.append("NXS_DATABASE__VERIFY_RUNTIME_ROLE must be true outside local/test")
        if self.auth.allow_ephemeral_signing_key:
            problems.append(
                "NXS_AUTH__ALLOW_EPHEMERAL_SIGNING_KEY must be false outside local/test"
            )
        if self.auth.signing_key is None and self.auth.signing_key_file is None:
            problems.append(
                "NXS_AUTH__SIGNING_KEY or NXS_AUTH__SIGNING_KEY_FILE is required outside local/test"
            )
        if self.auth.issuer is None or self.auth.audience is None:
            problems.append(
                "NXS_AUTH__ISSUER and NXS_AUTH__AUDIENCE must be explicit outside local/test"
            )
        if self.auth.argon2_time_cost < 3 or self.auth.argon2_memory_cost < 65_536:
            problems.append("Argon2id work factors are below the hardened minimum")
        if self.auth.rate_limit_backend == "local":
            problems.append("NXS_AUTH__RATE_LIMIT_BACKEND must not be 'local' outside local/test")
        if self.messaging.required and not self.events.require_jetstream:
            problems.append(
                "NXS_EVENTS__REQUIRE_JETSTREAM must be true outside local/test "
                "(durable business events must not fall back to core NATS)"
            )
        if self.events.publisher_poll_interval_seconds < 0.2:
            problems.append(
                "NXS_EVENTS__PUBLISHER_POLL_INTERVAL_SECONDS is below the hardened minimum (0.2s)"
            )
        if self.integrations.enabled and self.integrations.allow_ephemeral_vault_key:
            problems.append(
                "NXS_INTEGRATIONS__ALLOW_EPHEMERAL_VAULT_KEY must be false outside local/test"
            )
        if self.otp.enabled and self.otp.pepper is None and not self.otp.allow_ephemeral_pepper:
            problems.append(
                "NXS_OTP__PEPPER is required outside local/test (the OTP keyed verifier "
                "must not fall back to an ephemeral per-process pepper)"
            )
        if self.otp.allow_ephemeral_pepper:
            problems.append("NXS_OTP__ALLOW_EPHEMERAL_PEPPER must be false outside local/test")
        if problems:
            raise ValueError("unsafe security configuration: " + "; ".join(sorted(problems)))
        return self

    @model_validator(mode="after")
    def _production_safety(self) -> Self:
        if self.environment is not Environment.PRODUCTION:
            return self
        problems: list[str] = []
        if "*" in self.http.allowed_hosts or not self.http.allowed_hosts:
            problems.append("NXS_HTTP__ALLOWED_HOSTS must be an explicit non-wildcard list")
        if "*" in self.http.cors_allow_origins:
            problems.append("wildcard CORS origins are forbidden in production")
        if self.http.cors_allow_credentials and not self.http.cors_allow_origins:
            problems.append("CORS credentials require an explicit origin allow-list")
        if self.http.docs_enabled:
            problems.append("NXS_HTTP__DOCS_ENABLED must be false in production")
        if self.database.required and self.database.dsn is None:
            problems.append("NXS_DATABASE__DSN is required in production")
        if self.cache.required and self.cache.url is None:
            problems.append("NXS_CACHE__URL is required in production")
        if self.messaging.required and self.messaging.url is None:
            problems.append("NXS_MESSAGING__URL is required in production")
        if self.telemetry.mode == "disabled":
            problems.append("NXS_TELEMETRY__MODE must not be disabled in production")
        if problems:
            raise ValueError("unsafe production configuration: " + "; ".join(sorted(problems)))
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton. Call ``get_settings.cache_clear()`` in tests."""
    return Settings()
