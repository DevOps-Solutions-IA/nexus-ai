"""Argon2id password credential handling (NXS-AUTH-003).

Hashing goes exclusively through the argon2-cffi library primitive: per-password salt
and the Argon2id algorithm with versioned parameters are produced and verified by the
library, never by hand-rolled crypto. Stored values carry the encoded parameters, which
is the credential versioning seam — a future work-factor or algorithm migration
re-hashes on next successful verification without a schema change.
"""

from __future__ import annotations

import functools
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from nexus_ai.core.config import AuthSettings
from nexus_ai.core.errors import ConfigurationError

#: Algorithm tag persisted in the credential row (the versioned encoded string is
#: self-describing; this column makes the seam explicit and queryable).
CREDENTIAL_ALGORITHM: Final = "argon2id"
CREDENTIAL_VERSION: Final = 1


class PasswordHasherService:
    """Bounded Argon2id hashing and verification with constant-work dummy verify."""

    def __init__(self, *, time_cost: int, memory_cost: int, parallelism: int) -> None:
        self._parameters = {
            "time_cost": time_cost,
            "memory_cost": memory_cost,
            "parallelism": parallelism,
        }
        self._hasher = PasswordHasher(
            time_cost=time_cost,
            memory_cost=memory_cost,
            parallelism=parallelism,
            hash_len=32,
        )
        self._dummy_hash = self._hasher.hash("nexus-ai-dummy-password-0000")

    @property
    def parameters(self) -> dict[str, int]:
        return dict(self._parameters)

    @property
    def algorithm(self) -> str:
        return CREDENTIAL_ALGORITHM

    def hash(self, password: str) -> str:
        """Hash a bounded, already-policy-validated password (Argon2id encoded)."""
        return self._hasher.hash(password)

    def verify(self, encoded_hash: str, password: str) -> bool:
        """Constant-time verify. Malformed stored hashes fail closed (False)."""
        try:
            return self._hasher.verify(encoded_hash, password)
        except VerifyMismatchError, VerificationError, InvalidHashError, ValueError:
            return False

    def verify_dummy(self, password: str) -> None:
        """Burn a verification against a fixed dummy hash.

        Keeps unknown-email and wrong-password login paths indistinguishable in
        response content and close in wall-clock cost. The result is discarded.
        """
        self.verify(self._dummy_hash, password)

    @staticmethod
    def is_well_formed(encoded_hash: str) -> bool:
        try:
            PasswordHasher().check_needs_rehash(encoded_hash)
        except InvalidHashError, ValueError:
            return False
        return True


@functools.lru_cache(maxsize=1)
def build_password_hasher(settings: AuthSettings) -> PasswordHasherService:
    if settings.argon2_time_cost < 1 or settings.argon2_memory_cost < 8_192:
        raise ConfigurationError("Argon2id work factors are below the safe minimum")
    return PasswordHasherService(
        time_cost=settings.argon2_time_cost,
        memory_cost=settings.argon2_memory_cost,
        parallelism=settings.argon2_parallelism,
    )
