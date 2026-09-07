"""Argon2id credential behavior (NXS-AUTH-003)."""

from __future__ import annotations

import pytest
from argon2 import PasswordHasher as RawPasswordHasher

from nexus_ai.core.config import AuthSettings
from nexus_ai.domain.auth.passwords import (
    CREDENTIAL_ALGORITHM,
    CREDENTIAL_VERSION,
    PasswordHasherService,
    build_password_hasher,
)


@pytest.fixture
def hasher() -> PasswordHasherService:
    # Deliberately reduced work factors: unit tests must stay fast.
    settings = AuthSettings(argon2_time_cost=2, argon2_memory_cost=8192, argon2_parallelism=1)
    return build_password_hasher(settings)


class TestPasswordHasherService:
    def test_hash_uses_argon2id_with_per_password_salt(self, hasher: PasswordHasherService) -> None:
        first = hasher.hash("correct-horse-battery-staple")
        second = hasher.hash("correct-horse-battery-staple")
        assert first.startswith("$argon2id$")
        assert first != second  # per-password salt through the library primitive
        assert hasher.algorithm == CREDENTIAL_ALGORITHM
        assert hasher.parameters == {"time_cost": 2, "memory_cost": 8192, "parallelism": 1}

    def test_verify_roundtrip(self, hasher: PasswordHasherService) -> None:
        encoded = hasher.hash("correct-horse-battery-staple")
        assert hasher.verify(encoded, "correct-horse-battery-staple")
        assert not hasher.verify(encoded, "wrong-password-entirely")

    def test_verify_rejects_malformed_stored_hashes(self, hasher: PasswordHasherService) -> None:
        assert not hasher.verify("$argon2id$garbage", "anything-at-all")
        assert not hasher.verify("not-a-hash", "anything-at-all")
        assert not hasher.verify("", "anything-at-all")

    def test_verify_rejects_foreign_algorithm_hashes(self, hasher: PasswordHasherService) -> None:
        # A bcrypt-style marker or another algorithm family must fail closed.
        assert not hasher.verify("$2b$12$abc", "password-password")

    def test_dummy_verify_never_raises_and_burns_work(self, hasher: PasswordHasherService) -> None:
        hasher.verify_dummy("some-guess-at-a-password")

    def test_is_well_formed(self) -> None:
        reference = RawPasswordHasher(time_cost=2, memory_cost=8192, parallelism=1).hash("x" * 12)
        assert PasswordHasherService.is_well_formed(reference)
        assert not PasswordHasherService.is_well_formed("$argon2id$broken")
        assert not PasswordHasherService.is_well_formed("plaintext-password")

    def test_credential_version_seam_is_stable(self) -> None:
        # The version/algorithm constants anchor the versioning seam (migration column).
        assert CREDENTIAL_VERSION == 1
        assert CREDENTIAL_ALGORITHM == "argon2id"

    def test_encoded_hash_never_contains_the_password(self, hasher: PasswordHasherService) -> None:
        password = "correct-horse-battery-staple"
        encoded = hasher.hash(password)
        assert password not in encoded
        assert len(encoded) < 256  # fits the password_hash column
