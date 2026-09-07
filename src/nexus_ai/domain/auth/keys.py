"""Provider-neutral signing key management (NXS-AUTH-005).

The token layer talks only to the ``SigningKeyProvider`` protocol, so a future external
KMS/HSM/Vault integration replaces the local provider without touching token code.
Private key material is wrapped in ``SigningKeyData`` whose ``repr`` is redacted, is
never returned by API endpoints and never appears in logs. Rotation is supported by
keeping a verification map keyed by ``kid``: old keys keep verifying tokens until they
are removed from the provider configuration.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from nexus_ai.core.config import AuthSettings
from nexus_ai.core.errors import ConfigurationError


def derive_kid(public_bytes: bytes) -> str:
    """Deterministic key identifier from the public half of the key.

    The public half identifies a key without exposing private material.
    """
    return hashlib.sha256(public_bytes).hexdigest()[:16]


def _public_bytes(public_key: Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )


@dataclass(frozen=True, slots=True)
class SigningKeyData:
    """A private Ed25519 signing key plus its public identifier. repr-safe by design."""

    kid: str
    _private_key: Ed25519PrivateKey = field(repr=False)

    @property
    def private_key(self) -> Ed25519PrivateKey:
        return self._private_key

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self._private_key.public_key()


class SigningKeyProvider(Protocol):
    """Signing and verification boundary. Implementations own key storage and rotation."""

    def signing_key(self, kid: str | None = None) -> SigningKeyData: ...
    def verification_key(self, kid: str) -> Ed25519PublicKey: ...
    def all_kids(self) -> frozenset[str]: ...


class KeyNotFoundError(ConfigurationError):
    """A token references a key identifier this provider cannot resolve."""


class LocalEd25519KeyProvider:
    """In-process key provider for local/test deployments and small installations.

    Constructed with a primary signing key and an optional rotation map of older keys
    kept only for verification. Keys are derived from Ed25519 seeds supplied through
    validated configuration; they are never generated silently in hardened
    environments (the settings layer fails closed first).
    """

    def __init__(
        self,
        primary: Ed25519PrivateKey,
        *,
        verification: Mapping[str, Ed25519PublicKey] | None = None,
    ) -> None:
        self._primary = primary
        self._public_primary = primary.public_key()
        self._primary_kid = derive_kid(_public_bytes(self._public_primary))
        self._verification: dict[str, Ed25519PublicKey] = dict(verification or {})
        self._verification.setdefault(self._primary_kid, self._public_primary)

    def signing_key(self, kid: str | None = None) -> SigningKeyData:
        if kid is not None and kid != self._primary_kid:
            raise KeyNotFoundError(f"unknown signing key identifier {kid!r}")
        return SigningKeyData(kid=self._primary_kid, _private_key=self._primary)

    def verification_key(self, kid: str) -> Ed25519PublicKey:
        key = self._verification.get(kid)
        if key is None:
            raise KeyNotFoundError(f"unknown key identifier {kid!r}")
        return key

    def all_kids(self) -> frozenset[str]:
        return frozenset(self._verification)


def _key_from_seed(seed_hex: str) -> Ed25519PrivateKey:
    seed = bytes.fromhex(seed_hex)
    if len(seed) != 32:
        raise ConfigurationError("Ed25519 seeds must be exactly 32 bytes")
    return Ed25519PrivateKey.from_private_bytes(seed)


def build_key_provider(settings: AuthSettings) -> SigningKeyProvider:
    """Build the signing key provider from validated configuration (fail closed).

    Order of authority: inline seed, seed file, explicit ephemeral flag (local/test
    only — the settings validator rejects it elsewhere). Every other path raises.
    """
    verification: dict[str, Ed25519PublicKey] = {}
    for seed_hex in settings.verification_seed_list():
        verification_key = _key_from_seed(seed_hex).public_key()
        verification[derive_kid(_public_bytes(verification_key))] = verification_key

    if settings.signing_key is not None:
        primary = _key_from_seed(settings.signing_key.get_secret_value())
    elif settings.signing_key_file is not None:
        try:
            with open(settings.signing_key_file, encoding="utf-8") as handle:
                raw = handle.read().strip()
        except OSError as exc:
            raise ConfigurationError("cannot read NXS_AUTH__SIGNING_KEY_FILE") from exc
        if len(raw) != 64 or any(c not in "0123456789abcdefABCDEF" for c in raw):
            raise ConfigurationError("signing key file must hold a 64-character hex seed")
        primary = _key_from_seed(raw)
    elif settings.allow_ephemeral_signing_key:
        # Explicit local/test convenience only; never reached in staging/production.
        primary = Ed25519PrivateKey.generate()
    else:
        raise ConfigurationError(
            "no signing key configured; set NXS_AUTH__SIGNING_KEY or "
            "NXS_AUTH__ALLOW_EPHEMERAL_SIGNING_KEY=true (local/test only)"
        )
    return LocalEd25519KeyProvider(primary, verification=verification)
