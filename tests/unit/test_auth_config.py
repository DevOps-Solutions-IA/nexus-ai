"""AuthSettings validation and hardened-environment fail-closed rules (NXS-AUTH-005/009)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nexus_ai.core.config import AuthSettings, Settings

_GOOD_HARDENED_AUTH = dict(
    issuer="nexus-ai",
    audience="nexus-ai-backend",
    signing_key="0" * 64,
    allow_ephemeral_signing_key=False,
    rate_limit_backend="auto",
)


def _hardened(**auth_overrides: str) -> Settings:
    merged = dict(_GOOD_HARDENED_AUTH)
    for key, value in auth_overrides.items():
        merged[key] = value
    return Settings(
        environment="production",  # type: ignore[call-arg]
        http={"allowed_hosts": ("api.nexus-ai.dev",), "docs_enabled": False},
        database={"dsn": "postgresql+asyncpg://u:p@db.internal:5432/nexus"},
        cache={"url": "redis://cache.internal:6379/0"},
        messaging={"url": "nats://nats.internal:4222"},
        telemetry={"mode": "local"},
        auth=AuthSettings(**merged),
    )


class TestAuthSettingsValidation:
    def test_defaults_are_safe(self) -> None:
        settings = AuthSettings()
        assert settings.access_token_ttl_seconds == 900
        assert settings.refresh_token_ttl_seconds >= 3600
        assert 0 <= settings.clock_skew_seconds <= 120
        assert settings.allowed_algorithms == ("EdDSA",)
        assert settings.argon2_time_cost >= 3
        assert settings.argon2_memory_cost >= 65_536

    @pytest.mark.parametrize(
        "field,value",
        [
            ("access_token_ttl_seconds", 59),
            ("access_token_ttl_seconds", 3601),
            ("clock_skew_seconds", 121),
            ("login_max_failures", 0),
            ("min_password_length", 7),
        ],
    )
    def test_bounds_rejected(self, field: str, value: int) -> None:
        with pytest.raises(ValidationError):
            AuthSettings(**{field: value})

    def test_min_over_max_password_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not exceed"):
            AuthSettings(min_password_length=64, max_password_length=32)

    def test_non_eddsa_algorithms_rejected(self) -> None:
        with pytest.raises(ValidationError, match="EdDSA"):
            AuthSettings(allowed_algorithms=("RS256", "EdDSA"))

    @pytest.mark.parametrize("bad", ["short", "0" * 63, "g" * 64])
    def test_malformed_signing_key_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError, match="SIGNING_KEY"):
            AuthSettings(signing_key=bad)  # type: ignore[arg-type]

    def test_ephemeral_flag_conflicts_with_key_material(self) -> None:
        with pytest.raises(ValidationError, match="cannot be combined"):
            AuthSettings(signing_key="0" * 64, allow_ephemeral_signing_key=True)  # type: ignore[arg-type]


class TestHardenedEnvironmentRules:
    def test_valid_hardened_auth_config_accepted(self) -> None:
        assert _hardened().auth.signing_key is not None

    def test_ephemeral_keys_rejected(self) -> None:
        with pytest.raises(ValidationError, match="ALLOW_EPHEMERAL"):
            _hardened(allow_ephemeral_signing_key=True)

    def test_missing_signing_key_rejected(self) -> None:
        with pytest.raises(ValidationError, match="SIGNING_KEY"):
            Settings(  # type: ignore[call-arg]
                environment="staging",
                auth=AuthSettings(
                    issuer="nexus-ai", audience="nexus-ai-backend", rate_limit_backend="auto"
                ),
            )

    def test_missing_issuer_audience_rejected(self) -> None:
        with pytest.raises(ValidationError, match="ISSUER and NXS_AUTH__AUDIENCE"):
            _hardened(issuer=None)

    def test_weak_argon2_factors_rejected(self) -> None:
        with pytest.raises(ValidationError, match="work factors"):
            _hardened(argon2_time_cost=2)

    def test_local_rate_limit_backend_rejected(self) -> None:
        with pytest.raises(ValidationError, match="RATE_LIMIT_BACKEND"):
            _hardened(rate_limit_backend="local")

    def test_local_environment_allows_dev_conveniences(self) -> None:
        settings = Settings(  # type: ignore[call-arg]
            environment="local",
            auth=AuthSettings(allow_ephemeral_signing_key=True, rate_limit_backend="local"),
        )
        assert settings.auth.allow_ephemeral_signing_key
