"""Explicit configuration for a separate platform process."""

from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class PlatformAuditSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    database_dsn: SecretStr
    expected_role: Literal["nexus_audit_platform"] = "nexus_audit_platform"
    pool_size: int = Field(default=2, ge=1, le=8)
    database_timeout_seconds: float = Field(default=10, gt=0, le=60)
    batch_size: int = Field(default=50, ge=1, le=200)

    @model_validator(mode="after")
    def dedicated_connection(self) -> Self:
        parts = urlsplit(self.database_dsn.get_secret_value())
        if (
            parts.scheme not in {"postgresql", "postgresql+asyncpg"}
            or parts.username != self.expected_role
            or not parts.hostname
            or parts.query
            or parts.fragment
        ):
            raise ValueError("dedicated platform database role required")
        return self
