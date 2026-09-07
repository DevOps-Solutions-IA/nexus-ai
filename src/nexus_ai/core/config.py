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
