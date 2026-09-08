"""Bounded, allow-listed provider account configuration (NXS-P09 audit corrective D)."""

from __future__ import annotations

import pytest

from nexus_ai.core.config import ChannelsSettings
from nexus_ai.messaging.account_config import validate_account_configuration
from nexus_ai.messaging.entities import MessageChannel, SendMessageRequest
from nexus_ai.messaging.errors import MessagingConfigInvalidError

_SETTINGS = ChannelsSettings()


def _validate(channel: MessageChannel, provider: str, config: dict) -> dict:
    return validate_account_configuration(channel, provider, config, settings=_SETTINGS)


def test_oversized_configuration_is_rejected() -> None:
    with pytest.raises(MessagingConfigInvalidError):
        _validate(
            MessageChannel.EMAIL,
            "generic_http",
            {"api_base": "https://api.example.com/" + "x" * 600},
        )


def test_excessive_key_count_is_rejected() -> None:
    with pytest.raises(MessagingConfigInvalidError):
        _validate(
            MessageChannel.EMAIL,
            "generic_http",
            {f"key_{i}": "v" for i in range(_SETTINGS.max_account_config_keys + 1)},
        )


def test_excessive_nesting_is_rejected() -> None:
    deep: dict = {"api_base": "https://api.example.com"}
    node = deep
    for _ in range(_SETTINGS.max_account_config_depth + 2):
        node["nested"] = {}
        node = node["nested"]
    with pytest.raises(MessagingConfigInvalidError):
        _validate(MessageChannel.EMAIL, "generic_http", deep)


def test_unsupported_provider_key_is_rejected() -> None:
    with pytest.raises(MessagingConfigInvalidError):
        _validate(
            MessageChannel.WHATSAPP,
            "meta_cloud",
            {"graph_base": "https://graph.facebook.com", "arbitrary_url": "https://evil.example"},
        )
    with pytest.raises(MessagingConfigInvalidError):
        _validate(MessageChannel.SMS, "generic_http", {"headers": {"X-Evil": "1"}})


def test_non_https_or_credentialed_url_is_rejected() -> None:
    for bad in (
        "http://api.example.com",
        "https://user:pass@api.example.com",
        "https://api.example.com?x=1",
        "https://api.example.com#frag",
    ):
        with pytest.raises(MessagingConfigInvalidError):
            _validate(MessageChannel.EMAIL, "generic_http", {"api_base": bad})


def test_valid_configuration_is_accepted_per_channel() -> None:
    assert _validate(
        MessageChannel.WHATSAPP,
        "meta_cloud",
        {
            "graph_base": "https://graph.facebook.com",
            "api_version": "v20.0",
            "default_country": "1",
        },
    ) == {
        "graph_base": "https://graph.facebook.com",
        "api_version": "v20.0",
        "default_country": "1",
    }
    assert _validate(
        MessageChannel.EMAIL, "generic_http", {"api_base": "https://mail.example.com"}
    ) == {"api_base": "https://mail.example.com"}
    assert _validate(
        MessageChannel.SMS,
        "generic_http",
        {"api_base": "https://sms.example.com", "default_country": "44"},
    ) == {"api_base": "https://sms.example.com", "default_country": "44"}
    assert _validate(MessageChannel.SMS, "generic_http", {}) == {}


def test_nested_and_list_values_are_bounded() -> None:
    # a nested string that is too long is caught wherever it sits
    with pytest.raises(MessagingConfigInvalidError):
        _validate(
            MessageChannel.WHATSAPP,
            "meta_cloud",
            {"api_version": {"tag": "z" * 600}},
        )
    # an over-deep list nesting is caught
    with pytest.raises(MessagingConfigInvalidError):
        _validate(
            MessageChannel.WHATSAPP,
            "meta_cloud",
            {"api_version": [[[["too", "deep"]]]]},
        )
    # an unsupported value type is caught
    with pytest.raises(MessagingConfigInvalidError):
        _validate(MessageChannel.SMS, "generic_http", {"default_country": object()})  # type: ignore[dict-item]


def test_malformed_default_country_is_rejected() -> None:
    for bad in ("abc", "1234", 44):
        with pytest.raises(MessagingConfigInvalidError):
            _validate(MessageChannel.SMS, "generic_http", {"default_country": bad})  # type: ignore[dict-item]


def test_a_non_object_configuration_is_rejected() -> None:
    with pytest.raises(MessagingConfigInvalidError):
        _validate(MessageChannel.SMS, "generic_http", ["not", "a", "dict"])  # type: ignore[arg-type]


def test_send_request_still_cannot_control_url_method_or_headers() -> None:
    # regardless of account configuration, the send contract exposes no transport surface
    fields = set(SendMessageRequest.model_fields)
    assert not (fields & {"url", "method", "headers", "provider", "smtp", "graph_base", "api_base"})
