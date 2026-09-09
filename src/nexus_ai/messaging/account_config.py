"""Bounded, allow-listed messaging-account configuration (NXS-P09, ADR-0075).

A channel account's ``configuration`` is a small, provider-scoped settings object — NEVER
a per-send URL / method / header surface. Every key is allow-listed per
``(channel, provider)``; the whole object is bounded (key count, key length, value
length, nesting depth, serialised size); ``graph_base`` / ``api_base`` must be a plain
``https://`` origin and still passes the NXS-P07 ``DestinationPolicy`` /
``GovernedHttpExecutor`` at send time.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from nexus_ai.core.config import ChannelsSettings
from nexus_ai.messaging.entities import MessageChannel
from nexus_ai.messaging.errors import MessagingConfigInvalidError

#: The only configuration keys each (channel, provider) accepts.
_ALLOWED_KEYS: dict[tuple[MessageChannel, str], frozenset[str]] = {
    (MessageChannel.WHATSAPP, "meta_cloud"): frozenset(
        {"graph_base", "api_version", "default_country"}
    ),
    (MessageChannel.EMAIL, "generic_http"): frozenset({"api_base", "default_country"}),
    (MessageChannel.SMS, "generic_http"): frozenset({"api_base", "default_country"}),
}

_URL_KEYS = frozenset({"graph_base", "api_base"})
_COUNTRY_KEY = "default_country"


def _check_url(key: str, value: Any) -> None:
    if not isinstance(value, str):
        raise MessagingConfigInvalidError(f"{key!r} must be an https URL string")
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.query
        or parts.fragment
    ):
        raise MessagingConfigInvalidError(
            f"{key!r} must be a plain https origin (no credentials, query or fragment)"
        )


def _check_depth(value: Any, *, limit: int, depth: int = 1) -> None:
    if depth > limit:
        raise MessagingConfigInvalidError("the configuration is nested too deeply")
    if isinstance(value, dict):
        for item in value.values():
            _check_depth(item, limit=limit, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_depth(item, limit=limit, depth=depth + 1)


def _check_value_sizes(value: Any, *, max_string: int) -> None:
    if isinstance(value, str):
        if len(value) > max_string:
            raise MessagingConfigInvalidError(
                f"a configuration string exceeds {max_string} characters"
            )
    elif isinstance(value, dict):
        for item in value.values():
            _check_value_sizes(item, max_string=max_string)
    elif isinstance(value, list):
        for item in value:
            _check_value_sizes(item, max_string=max_string)
    elif not isinstance(value, (int, float, bool)) and value is not None:
        raise MessagingConfigInvalidError("a configuration value has an unsupported type")


def validate_account_configuration(
    channel: MessageChannel,
    provider: str,
    configuration: dict[str, Any],
    *,
    settings: ChannelsSettings,
) -> dict[str, Any]:
    """Return a bounded, allow-listed copy of ``configuration`` or raise
    :class:`MessagingConfigInvalidError`."""
    if not isinstance(configuration, dict):
        raise MessagingConfigInvalidError("the configuration must be a JSON object")
    if len(configuration) > settings.max_account_config_keys:
        raise MessagingConfigInvalidError(
            f"the configuration has more than {settings.max_account_config_keys} keys"
        )

    allowed = _ALLOWED_KEYS.get((channel, provider))
    for key in configuration:
        if not isinstance(key, str) or len(key) > settings.max_account_config_key_length:
            raise MessagingConfigInvalidError("a configuration key is malformed or too long")
        if allowed is not None and key not in allowed:
            raise MessagingConfigInvalidError(
                f"{key!r} is not a supported configuration key for {channel.value} / {provider}"
            )

    _check_depth(configuration, limit=settings.max_account_config_depth)
    _check_value_sizes(configuration, max_string=settings.max_account_config_value_length)

    serialised = json.dumps(configuration, sort_keys=True, separators=(",", ":"))
    if len(serialised.encode("utf-8")) > settings.max_account_config_bytes:
        raise MessagingConfigInvalidError(
            f"the serialised configuration exceeds {settings.max_account_config_bytes} bytes"
        )

    for key in _URL_KEYS & set(configuration):
        _check_url(key, configuration[key])
    country = configuration.get(_COUNTRY_KEY)
    if country is not None and (
        not isinstance(country, str) or not country.isdigit() or len(country) > 3
    ):
        raise MessagingConfigInvalidError("default_country must be a 1-3 digit country code")

    return dict(configuration)
