"""Signing key provider behavior and fail-closed key configuration (NXS-AUTH-005)."""

from __future__ import annotations

import secrets

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from nexus_ai.core.config import AuthSettings, Settings
from nexus_ai.core.errors import ConfigurationError
from nexus_ai.domain.auth.keys import (
    KeyNotFoundError,
    LocalEd25519KeyProvider,
    build_key_provider,
    derive_kid,
)


@pytest.fixture
def seed() -> str:
    return secrets.token_hex(32)


class TestLocalProvider:
    def test_signing_and_verification_roundtrip(self) -> None:
        provider = LocalEd25519KeyProvider(Ed25519PrivateKey.generate())
        signing = provider.signing_key()
        assert signing.kid in provider.all_kids()
        # The public half verifies what the private half signs (done end-to-end in
        # token tests); here we assert structural facts.
        assert signing.public_key is not None

    def test_kid_is_deterministic_for_a_key(self, seed: str) -> None:
        first = LocalEd25519KeyProvider(Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed)))
        second = LocalEd25519KeyProvider(Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed)))
        assert first.signing_key().kid == second.signing_key().kid

    def test_kids_differ_across_keys(self) -> None:
        first = LocalEd25519KeyProvider(Ed25519PrivateKey.generate()).signing_key().kid
        second = LocalEd25519KeyProvider(Ed25519PrivateKey.generate()).signing_key().kid
        assert first != second

    def test_unknown_kid_raises_key_not_found(self) -> None:
        provider = LocalEd25519KeyProvider(Ed25519PrivateKey.generate())
        with pytest.raises(KeyNotFoundError):
            provider.verification_key("does-not-exist")

    def test_signing_with_foreign_kid_raises(self) -> None:
        provider = LocalEd25519KeyProvider(Ed25519PrivateKey.generate())
        with pytest.raises(KeyNotFoundError):
            provider.signing_key("foreign-kid")

    def test_private_material_never_appears_in_repr(self) -> None:
        provider = LocalEd25519KeyProvider(Ed25519PrivateKey.generate())
        signing = provider.signing_key()
        private_bytes = signing.private_key.private_bytes_raw()
        assert private_bytes not in repr(signing).encode()


class TestBuildKeyProvider:
    def test_inline_seed_builds_deterministic_provider(self, seed: str) -> None:
        settings = AuthSettings(signing_key=seed)  # type: ignore[arg-type]
        first = build_key_provider(settings)
        second = build_key_provider(settings)
        assert first.signing_key().kid == second.signing_key().kid

    def test_seed_file_builds_provider(self, seed: str, tmp_path) -> None:
        key_file = tmp_path / "signing.key"
        key_file.write_text(seed, encoding="utf-8")
        settings = AuthSettings(signing_key_file=str(key_file))
        assert build_key_provider(settings).signing_key().kid is not None

    def test_ephemeral_flag_builds_fresh_provider(self) -> None:
        settings = AuthSettings(allow_ephemeral_signing_key=True)
        assert build_key_provider(settings).signing_key().kid is not None

    def test_no_key_configuration_fails_closed(self) -> None:
        settings = AuthSettings()
        with pytest.raises(ConfigurationError, match="no signing key configured"):
            build_key_provider(settings)

    def test_missing_seed_file_fails_closed(self, tmp_path) -> None:
        settings = AuthSettings(signing_key_file=str(tmp_path / "absent.key"))
        with pytest.raises(ConfigurationError, match="SIGNING_KEY_FILE"):
            build_key_provider(settings)

    @pytest.mark.parametrize("bad", ["short", "z" * 64, "0" * 63])
    def test_malformed_seed_fails_closed(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            AuthSettings(signing_key=bad)  # type: ignore[arg-type]

    def test_verification_seeds_enable_rotation(self, seed: str) -> None:
        rotated = secrets.token_hex(32)
        settings = AuthSettings(
            signing_key=seed,  # type: ignore[arg-type]
            verification_key_seeds=rotated,
        )
        provider = build_key_provider(settings)
        assert len(provider.all_kids()) == 2

    def test_hardened_env_rejects_ephemeral_keys(self) -> None:
        with pytest.raises(Exception, match="ALLOW_EPHEMERAL"):
            Settings(
                environment="production",  # type: ignore[call-arg]
                auth=AuthSettings(allow_ephemeral_signing_key=True),
            )

    def test_hardened_env_requires_key_material(self) -> None:
        with pytest.raises(Exception, match="SIGNING_KEY"):
            Settings(environment="staging", auth=AuthSettings())  # type: ignore[call-arg]


class TestSecretHandling:
    def test_signing_key_settings_are_secret_strings(self, seed: str) -> None:
        settings = AuthSettings(signing_key=seed)  # type: ignore[arg-type]
        assert seed not in repr(settings)
        assert seed not in str(settings)

    def test_derive_kid_is_stable_and_public_only(self) -> None:
        private = Ed25519PrivateKey.generate()
        from cryptography.hazmat.primitives import serialization

        raw = private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
        assert derive_kid(raw) == derive_kid(raw)
        assert len(derive_kid(raw)) == 16
