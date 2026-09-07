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
    pool_size: int = Field(default=5, ge=1, le=100)
    max_overflow: int = Field(default=5, ge=0, le=100)
    pool_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    pool_recycle_seconds: int = Field(default=1800, ge=60, le=86400)
    connect_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    command_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    required: bool = True

    @field_validator("dsn")
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

    def async_dsn(self) -> str:
        if self.dsn is None:
            raise ValueError("database DSN is not configured")
        raw = self.dsn.get_secret_value()
        if raw.startswith("postgresql://"):
            raw = raw.replace("postgresql://", "postgresql+asyncpg://", 1)
        return raw


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
