"""Finite, fail-closed settings for the independent Sentinel connection."""

from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class SentinelSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    database_dsn: SecretStr | None = None
    expected_role: Literal["nexus_sentinel"] = "nexus_sentinel"
    pool_size: int = Field(default=3, ge=1, le=16)
    database_timeout_seconds: float = Field(default=10, gt=0, le=60)
    adapter_timeout_seconds: float = Field(default=5, gt=0, le=30)
    model_timeout_seconds: float = Field(default=30, gt=0, le=120)
    execution_timeout_seconds: float = Field(default=30, gt=0, le=120)
    signal_batch_size: int = Field(default=50, ge=1, le=200)
    max_signal_bytes: int = Field(default=16384, ge=1024, le=32768)
    max_facts_bytes: int = Field(default=4096, ge=128, le=8192)
    max_evidence_refs: int = Field(default=16, ge=1, le=32)
    max_evidence_snippet_bytes: int = Field(default=512, ge=1, le=2048)
    max_incidents_scanned: int = Field(default=100, ge=1, le=500)
    max_findings_per_incident: int = Field(default=20, ge=1, le=100)
    max_proposals_per_incident: int = Field(default=20, ge=1, le=100)
    max_active_executions: int = Field(default=5, ge=1, le=32)
    lease_duration_seconds: int = Field(default=60, ge=5, le=300)
    safe_read_only_retries: int = Field(default=1, ge=0, le=3)
    cleanup_batch_size: int = Field(default=100, ge=1, le=500)
    reasoning_enabled: bool = Field(default=False, strict=True)
    mutable_actions_enabled: bool = False

    @model_validator(mode="after")
    def safe_configuration(self) -> Self:
        if self.enabled and self.database_dsn is None:
            raise ValueError("Sentinel requires its own database DSN")
        if self.database_dsn is not None:
            parts = urlsplit(self.database_dsn.get_secret_value())
            if (
                parts.scheme not in {"postgresql", "postgresql+asyncpg"}
                or parts.username != self.expected_role
                or not parts.hostname
                or parts.query
                or parts.fragment
            ):
                raise ValueError("Sentinel DSN must name the dedicated role and no overrides")
        if self.max_facts_bytes >= self.max_signal_bytes:
            raise ValueError("facts must fit inside the signal budget")
        if self.lease_duration_seconds <= self.execution_timeout_seconds:
            raise ValueError("lease must exceed the bounded execution timeout")
        return self
