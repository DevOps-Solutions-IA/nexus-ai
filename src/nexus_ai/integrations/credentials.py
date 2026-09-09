"""Credential-reference / vault seam (NXS-INT-001, ADR-0055).

An :class:`~nexus_ai.integrations.entities.AuthProfile` never holds a secret — it holds an
opaque ``credential_ref``. Resolving that reference to usable secret material is the ONLY
job of this module, and it is a seam: P07 ships the interface plus a local
encrypted-at-rest implementation; a production external vault (AWS/GCP/Vault) is a later
drop-in that satisfies the same :class:`VaultClient` protocol.

Hard rules:

* secret material is NEVER stored in PostgreSQL in plaintext — the local implementation
  persists Fernet ciphertext (AES-128-CBC + HMAC-SHA256) only;
* :class:`SecretMaterial` redacts itself in ``repr``/``str`` and is never a Pydantic model
  (so it cannot be dumped through ``model_dump``); nothing logs its fields;
* the resolver returns material to the auth layer and the executor and to nobody else —
  it never crosses the API boundary and never reaches the LLM.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from nexus_ai.integrations.errors import IntegrationCredentialUnavailableError


class CredentialType(StrEnum):
    API_KEY = "API_KEY"
    BEARER_TOKEN = "BEARER_TOKEN"  # noqa: S105 - enum member name, not a secret
    BASIC_AUTH = "BASIC_AUTH"
    OAUTH2_CLIENT = "OAUTH2_CLIENT"
    HMAC_SECRET = "HMAC_SECRET"  # noqa: S105 - enum member name, not a secret
    #: A bounded bag of named provider-secret fields with no fixed schema, used by the
    #: NXS-P09 messaging channels (e.g. an API token plus a webhook-signing secret plus a
    #: challenge verify token). Same vault, same encryption, same seam — no fixed fields.
    PROVIDER_SECRET_SET = "PROVIDER_SECRET_SET"  # noqa: S105 - enum member name, not a secret


#: The field names each credential type must carry. Enforced on store and on resolve.
_REQUIRED_FIELDS: dict[CredentialType, frozenset[str]] = {
    CredentialType.API_KEY: frozenset({"api_key"}),
    CredentialType.BEARER_TOKEN: frozenset({"token"}),
    CredentialType.BASIC_AUTH: frozenset({"username", "password"}),
    CredentialType.OAUTH2_CLIENT: frozenset({"client_id", "client_secret"}),
    CredentialType.HMAC_SECRET: frozenset({"secret"}),
    CredentialType.PROVIDER_SECRET_SET: frozenset(),
}

_MAX_FIELD_BYTES = 8192


@dataclass(frozen=True, slots=True)
class SecretMaterial:
    """Opaque secret holder. Redacts on ``repr``/``str``; never a serialisable model."""

    credential_type: CredentialType
    _fields: Mapping[str, str]

    def __post_init__(self) -> None:
        required = _REQUIRED_FIELDS[self.credential_type]
        missing = required - set(self._fields)
        if missing:
            raise IntegrationCredentialUnavailableError(
                f"credential is missing required fields for {self.credential_type}"
            )
        for value in self._fields.values():
            if not isinstance(value, str) or len(value.encode("utf-8")) > _MAX_FIELD_BYTES:
                raise IntegrationCredentialUnavailableError("credential field is invalid")

    def field(self, name: str) -> str:
        try:
            return self._fields[name]
        except KeyError:
            raise IntegrationCredentialUnavailableError(
                f"credential has no {name!r} field"
            ) from None

    def as_transport_fields(self) -> dict[str, str]:
        """The raw fields, for the auth layer / executor ONLY. Never log this."""
        return dict(self._fields)

    def __repr__(self) -> str:
        return f"SecretMaterial(type={self.credential_type}, fields=<redacted:{len(self._fields)}>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class EncryptedSecret:
    credential_type: CredentialType
    ciphertext: str


class VaultClient(Protocol):
    """The credential vault contract. Every implementation is tenant-scoped by argument."""

    async def get_secret(self, organization_id: UUID, ref: str) -> SecretMaterial: ...

    async def store_secret(
        self, organization_id: UUID, ref: str, material: SecretMaterial
    ) -> None: ...

    async def delete_secret(self, organization_id: UUID, ref: str) -> None: ...

    async def has_secret(self, organization_id: UUID, ref: str) -> bool: ...


class EncryptedSecretStore(Protocol):
    """Durable ciphertext persistence. Satisfied by the P07 repository; keeps this module
    free of ORM imports."""

    async def get(self, organization_id: UUID, ref: str) -> EncryptedSecret | None: ...

    async def put(self, organization_id: UUID, ref: str, secret: EncryptedSecret) -> None: ...

    async def delete(self, organization_id: UUID, ref: str) -> bool: ...


def build_fernet(keys: list[str]) -> MultiFernet:
    """Build a MultiFernet from one or more url-safe base64 32-byte keys (first encrypts,
    all decrypt — supports rotation)."""
    if not keys:
        raise IntegrationCredentialUnavailableError("no vault encryption key is configured")
    try:
        return MultiFernet([Fernet(key.encode("ascii")) for key in keys])
    except (ValueError, TypeError) as exc:
        raise IntegrationCredentialUnavailableError(
            "the vault encryption key is malformed (expect url-safe base64, 32 bytes)"
        ) from exc


class LocalEncryptedVault:
    """Encrypted-at-rest vault backed by an :class:`EncryptedSecretStore`.

    Serialises the secret fields to a compact form, encrypts with Fernet and persists only
    the ciphertext. Decryption failures (wrong/rotated-out key, tampered row) surface as a
    stable ``NXS_INT_CREDENTIAL_UNAVAILABLE`` — never a raw crypto error.
    """

    _SEP = "\x1f"

    def __init__(self, store: EncryptedSecretStore, fernet: MultiFernet) -> None:
        self._store = store
        self._fernet = fernet

    def _encode(self, material: SecretMaterial) -> str:
        parts: list[str] = []
        for name, value in sorted(material.as_transport_fields().items()):
            if self._SEP in name or self._SEP in value or "=" in name:
                raise IntegrationCredentialUnavailableError("credential field name is invalid")
            parts.append(f"{name}={value}")
        blob = self._SEP.join(parts).encode("utf-8")
        return self._fernet.encrypt(blob).decode("ascii")

    def _decode(self, secret: EncryptedSecret) -> SecretMaterial:
        try:
            blob = self._fernet.decrypt(secret.ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise IntegrationCredentialUnavailableError(
                "the stored credential could not be decrypted"
            ) from exc
        fields: dict[str, str] = {}
        for part in blob.split(self._SEP):
            name, _, value = part.partition("=")
            fields[name] = value
        return SecretMaterial(secret.credential_type, fields)

    async def get_secret(self, organization_id: UUID, ref: str) -> SecretMaterial:
        row = await self._store.get(organization_id, ref)
        if row is None:
            raise IntegrationCredentialUnavailableError(
                "no credential is stored for this reference"
            )
        return self._decode(row)

    async def store_secret(self, organization_id: UUID, ref: str, material: SecretMaterial) -> None:
        await self._store.put(
            organization_id,
            ref,
            EncryptedSecret(material.credential_type, self._encode(material)),
        )

    async def delete_secret(self, organization_id: UUID, ref: str) -> None:
        await self._store.delete(organization_id, ref)

    async def has_secret(self, organization_id: UUID, ref: str) -> bool:
        return await self._store.get(organization_id, ref) is not None


class InMemoryVault:
    """A process-local vault for unit tests. Not durable, not encrypted — never wired
    into the running application."""

    def __init__(self) -> None:
        self._data: dict[tuple[UUID, str], SecretMaterial] = {}

    async def get_secret(self, organization_id: UUID, ref: str) -> SecretMaterial:
        try:
            return self._data[(organization_id, ref)]
        except KeyError:
            raise IntegrationCredentialUnavailableError(
                "no credential is stored for this reference"
            ) from None

    async def store_secret(self, organization_id: UUID, ref: str, material: SecretMaterial) -> None:
        self._data[(organization_id, ref)] = material

    async def delete_secret(self, organization_id: UUID, ref: str) -> None:
        self._data.pop((organization_id, ref), None)

    async def has_secret(self, organization_id: UUID, ref: str) -> bool:
        return (organization_id, ref) in self._data
