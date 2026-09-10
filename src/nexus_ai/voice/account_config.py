"""Bounded voice provider account configuration (NXS-P12, ADR-0087).

``CreateVoiceAccountRequest.configuration`` is not a free-form ``dict[str, Any]``: it is
an allow-listed, typed, bounded surface per provider. A voice account cannot become a
smuggling vector for an arbitrary provider endpoint, a WebSocket URL, an ARI command or a
media target. The media-gateway host / port are validated again (SSRF) by
:mod:`nexus_ai.voice.bridge` at session start.
"""

from __future__ import annotations

import json
from typing import Any

from nexus_ai.core.config import VoiceSettings
from nexus_ai.voice.bridge import GATEWAY_HOST_KEY, GATEWAY_PORT_KEY, resolve_gateway
from nexus_ai.voice.entities import VoiceProvider
from nexus_ai.voice.errors import VoiceConfigInvalidError

#: Per-provider allow-list of configuration keys. Anything else is rejected.
_ALLOWED_KEYS: dict[VoiceProvider, frozenset[str]] = {
    VoiceProvider.ELEVENLABS: frozenset(
        {GATEWAY_HOST_KEY, GATEWAY_PORT_KEY, "agent_prefix", "region"}
    ),
    VoiceProvider.FAKE: frozenset({GATEWAY_HOST_KEY, GATEWAY_PORT_KEY}),
}

_REGIONS = frozenset({"us", "eu", "global"})


def validate_voice_account_configuration(
    provider: VoiceProvider,
    configuration: dict[str, Any],
    *,
    settings: VoiceSettings,
) -> dict[str, Any]:
    """Validate and normalize a voice account configuration, or fail closed with
    :class:`VoiceConfigInvalidError`."""
    if not isinstance(configuration, dict):
        raise VoiceConfigInvalidError("configuration must be a JSON object")
    if len(configuration) > settings.max_account_config_keys:
        raise VoiceConfigInvalidError(
            f"at most {settings.max_account_config_keys} configuration keys are allowed"
        )
    allowed = _ALLOWED_KEYS[provider]
    clean: dict[str, Any] = {}
    for key, value in configuration.items():
        if not isinstance(key, str) or len(key) > settings.max_account_config_key_length:
            raise VoiceConfigInvalidError(f"configuration key {key!r} is invalid")
        if key not in allowed:
            raise VoiceConfigInvalidError(
                f"{provider.value} does not support the configuration key {key!r}"
            )
        clean[key] = _validate_value(key, value, settings=settings)

    if GATEWAY_HOST_KEY in clean or GATEWAY_PORT_KEY in clean:
        # Fully validate the pair now (SSRF) so a bad gateway is rejected at config time.
        resolve_gateway(clean)

    serialized = json.dumps(clean, separators=(",", ":"), sort_keys=True)
    if len(serialized.encode("utf-8")) > settings.max_account_config_bytes:
        raise VoiceConfigInvalidError("the configuration object is too large")
    return clean


def _validate_value(key: str, value: Any, *, settings: VoiceSettings) -> Any:
    if key == GATEWAY_PORT_KEY:
        if isinstance(value, bool) or not isinstance(value, int):
            raise VoiceConfigInvalidError(f"{key!r} must be an integer port")
        return value
    if not isinstance(value, str):
        raise VoiceConfigInvalidError(f"configuration value for {key!r} must be a string")
    if len(value) > settings.max_account_config_value_length:
        raise VoiceConfigInvalidError(f"configuration value for {key!r} is too long")
    if any(ord(ch) < 0x20 for ch in value):
        raise VoiceConfigInvalidError(
            f"configuration value for {key!r} contains control characters"
        )
    if key == "region" and value not in _REGIONS:
        raise VoiceConfigInvalidError(f"{key!r} must be one of {sorted(_REGIONS)}")
    if key == "agent_prefix" and (
        len(value) > 32 or not value.replace("_", "a").replace("-", "a").isalnum()
    ):
        raise VoiceConfigInvalidError(f"{key!r} must be a short alphanumeric prefix")
    return value
