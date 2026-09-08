"""Apply a typed AuthProfile to an outbound request (NXS-INT-001).

The auth layer is the only component that turns a ``credential_ref`` into real secret
material (via the vault seam) and injects it. It is header-injection safe: it never sets a
reserved header for anything other than its own ``Authorization`` (Bearer / Basic), an
API-key header is rejected at config time if it names a reserved header, and secret values
are newline-stripped. OAuth2 client-credentials tokens are fetched through the governed
executor and cached until shortly before expiry.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from uuid import UUID

from nexus_ai.core.logging import get_logger
from nexus_ai.integrations.credentials import CredentialType, SecretMaterial, VaultClient
from nexus_ai.integrations.destination import DestinationPolicy
from nexus_ai.integrations.entities import ApiKeyLocation, AuthProfile, AuthProfileType
from nexus_ai.integrations.errors import (
    IntegrationAuthProfileInvalidError,
    IntegrationCredentialUnavailableError,
)
from nexus_ai.integrations.executor import GovernedHttpExecutor, OutboundRequest

_EXPECTED_CREDENTIAL: dict[AuthProfileType, CredentialType] = {
    AuthProfileType.API_KEY: CredentialType.API_KEY,
    AuthProfileType.BEARER_TOKEN: CredentialType.BEARER_TOKEN,
    AuthProfileType.BASIC: CredentialType.BASIC_AUTH,
    AuthProfileType.OAUTH2_CLIENT_CREDENTIALS: CredentialType.OAUTH2_CLIENT,
    AuthProfileType.CUSTOM_HEADER: CredentialType.API_KEY,
}


@dataclass(frozen=True, slots=True)
class PreparedAuth:
    headers: dict[str, str] = field(default_factory=dict)
    query_params: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class _CachedToken:
    value: str
    expires_at: float


def _clean(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise IntegrationAuthProfileInvalidError("a credential value is malformed")
    return value


class AuthProfileApplier:
    def __init__(
        self,
        vault: VaultClient,
        policy: DestinationPolicy,
        executor: GovernedHttpExecutor,
    ) -> None:
        self._vault = vault
        self._policy = policy
        self._executor = executor
        self._log = get_logger("nexus_ai.integrations.auth")
        self._token_cache: dict[tuple[UUID, str], _CachedToken] = {}

    async def prepare(self, organization_id: UUID, profile: AuthProfile) -> PreparedAuth:
        if profile.profile_type is AuthProfileType.NONE:
            return PreparedAuth()
        if profile.credential_ref is None:  # pragma: no cover - model_validator guarantees this
            raise IntegrationAuthProfileInvalidError("the auth profile has no credential_ref")

        material = await self._vault.get_secret(organization_id, profile.credential_ref)
        expected = _EXPECTED_CREDENTIAL[profile.profile_type]
        if material.credential_type is not expected:
            raise IntegrationCredentialUnavailableError(
                "the referenced credential is the wrong type for this auth profile"
            )

        if profile.profile_type in (AuthProfileType.API_KEY, AuthProfileType.CUSTOM_HEADER):
            return self._api_key(profile, material)
        if profile.profile_type is AuthProfileType.BEARER_TOKEN:
            token = _clean(material.field("token"))
            return PreparedAuth(headers={"Authorization": f"Bearer {token}"})
        if profile.profile_type is AuthProfileType.BASIC:
            raw = f"{material.field('username')}:{material.field('password')}"
            token = base64.b64encode(raw.encode("utf-8")).decode("ascii")
            return PreparedAuth(headers={"Authorization": f"Basic {token}"})
        return await self._oauth2(organization_id, profile, material)

    def _api_key(self, profile: AuthProfile, material: SecretMaterial) -> PreparedAuth:
        key = _clean(material.field("api_key"))
        value = f"{profile.value_prefix}{key}" if profile.value_prefix else key
        if profile.api_key_location is ApiKeyLocation.QUERY:
            if profile.query_param_name is None:  # pragma: no cover - model_validator guarantees
                raise IntegrationAuthProfileInvalidError("query key without a parameter name")
            return PreparedAuth(query_params={profile.query_param_name: value})
        if profile.header_name is None:  # pragma: no cover - model_validator guarantees
            raise IntegrationAuthProfileInvalidError("header key without a header name")
        return PreparedAuth(headers={profile.header_name: value})

    async def _oauth2(
        self, organization_id: UUID, profile: AuthProfile, material: SecretMaterial
    ) -> PreparedAuth:
        if profile.token_url is None:  # pragma: no cover - model_validator guarantees
            raise IntegrationAuthProfileInvalidError("OAuth2 profile without a token_url")
        cache_key = (organization_id, profile.credential_ref or "")
        cached = self._token_cache.get(cache_key)
        now = time.time()
        if cached is not None and cached.expires_at - 30 > now:
            return PreparedAuth(headers={"Authorization": f"Bearer {cached.value}"})

        self._policy.validate_url(profile.token_url)
        form = {
            "grant_type": "client_credentials",
            "client_id": material.field("client_id"),
            "client_secret": material.field("client_secret"),
        }
        if profile.oauth_scope:
            form["scope"] = profile.oauth_scope
        body = "&".join(f"{k}={_percent(v)}" for k, v in form.items()).encode("ascii")
        response = await self._executor.send(
            OutboundRequest(
                method="POST",
                url=profile.token_url,
                headers={"Accept": "application/json"},
                body=body,
                content_type="application/x-www-form-urlencoded",
            )
        )
        if response.status_code != 200:
            raise IntegrationCredentialUnavailableError(
                "the OAuth2 token endpoint rejected the client-credentials request"
            )
        try:
            payload = json.loads(response.body.decode("utf-8"))
            access_token = _clean(str(payload["access_token"]))
            expires_in = float(payload.get("expires_in", 300))
        except (ValueError, KeyError, TypeError) as exc:
            raise IntegrationCredentialUnavailableError(
                "the OAuth2 token response is malformed"
            ) from exc
        self._token_cache[cache_key] = _CachedToken(
            access_token, now + max(30.0, min(expires_in, 86_400.0))
        )
        return PreparedAuth(headers={"Authorization": f"Bearer {access_token}"})


def _percent(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")
