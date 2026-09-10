"""Bounded telephony account configuration (NXS-P11, ADR-0084).

``CreateAccountRequest.configuration`` is not a free-form ``dict[str, Any]``: it is an
allow-listed, typed, bounded surface per provider. A telephony account cannot become a
smuggling vector for an arbitrary ARI base URL, an AMI action, a dialplan fragment or a
shell string. The ARI base URL, when present, is still forced through the NXS-P07
``DestinationPolicy`` / ``GovernedHttpExecutor`` at call time.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from nexus_ai.core.config import TelephonySettings
from nexus_ai.telephony.entities import TelephonyProvider
from nexus_ai.telephony.errors import TelephonyConfigInvalidError

#: Per-provider allow-list of configuration keys. Anything else is rejected.
_ALLOWED_KEYS: dict[TelephonyProvider, frozenset[str]] = {
    TelephonyProvider.ASTERISK: frozenset(
        {"ari_base", "ari_app", "stasis_app", "default_country", "sip_endpoint_prefix"}
    ),
    TelephonyProvider.FAKE: frozenset({"default_country"}),
}

_URL_KEYS = frozenset({"ari_base"})
_COUNTRY_KEYS = frozenset({"default_country"})
_ALIAS_PREFIX_KEYS = frozenset({"sip_endpoint_prefix"})


def validate_account_configuration(
    provider: TelephonyProvider,
    configuration: dict[str, Any],
    *,
    settings: TelephonySettings,
) -> dict[str, Any]:
    """Validate and normalize an account configuration. Raises
    :class:`TelephonyConfigInvalidError` on an unknown key, a bad value, or a bound
    violation."""
    if not isinstance(configuration, dict):
        raise TelephonyConfigInvalidError("configuration must be a JSON object")
    if len(configuration) > settings.max_account_config_keys:
        raise TelephonyConfigInvalidError(
            f"at most {settings.max_account_config_keys} configuration keys are allowed"
        )
    allowed = _ALLOWED_KEYS[provider]
    clean: dict[str, Any] = {}
    for key, value in configuration.items():
        if not isinstance(key, str) or len(key) > settings.max_account_config_key_length:
            raise TelephonyConfigInvalidError(f"configuration key {key!r} is invalid")
        if key not in allowed:
            raise TelephonyConfigInvalidError(
                f"{provider.value} does not support the configuration key {key!r}"
            )
        clean[key] = _validate_value(key, value, settings=settings)

    serialized = json.dumps(clean, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > settings.max_account_config_bytes:
        raise TelephonyConfigInvalidError("the configuration object is too large")
    return clean


def _validate_value(key: str, value: Any, *, settings: TelephonySettings) -> Any:
    if not isinstance(value, str):
        raise TelephonyConfigInvalidError(f"configuration value for {key!r} must be a string")
    if len(value) > settings.max_account_config_value_length:
        raise TelephonyConfigInvalidError(f"configuration value for {key!r} is too long")
    if any(ord(ch) < 0x20 for ch in value):
        raise TelephonyConfigInvalidError(
            f"configuration value for {key!r} contains control characters"
        )
    if key in _URL_KEYS:
        return _validate_https_origin(key, value)
    if key in _COUNTRY_KEYS:
        if not value.isdigit() or not (1 <= len(value) <= 3) or value[0] == "0":
            raise TelephonyConfigInvalidError(
                f"{key!r} must be a 1-3 digit E.164 country calling code"
            )
        return value
    if key in _ALIAS_PREFIX_KEYS:
        if not value.replace("_", "a").replace("-", "a").isalnum() or len(value) > 32:
            raise TelephonyConfigInvalidError(f"{key!r} must be a short alphanumeric prefix")
        return value
    return value


def _validate_https_origin(key: str, value: Any) -> str:
    try:
        parts = urlsplit(str(value))
    except ValueError as exc:
        raise TelephonyConfigInvalidError(f"{key!r} is not a valid URL") from exc
    if parts.scheme != "https" or not parts.hostname:
        raise TelephonyConfigInvalidError(f"{key!r} must be an https:// origin")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise TelephonyConfigInvalidError(
            f"{key!r} must be a bare origin with no credentials, query or fragment"
        )
    path = parts.path.rstrip("/")
    return f"https://{parts.netloc}{path}"
