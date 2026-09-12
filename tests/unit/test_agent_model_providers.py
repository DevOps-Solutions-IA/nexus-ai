"""The OpenAI-compatible model adapter (NXS-P13, ADR-0091) — CONTRACT-CERTIFIED.

Exercised against a fake transport returning canned chat-completions JSON. No live
credential, no network: this proves the wire contract mapping, not a real provider.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from nexus_ai.agents.models.base import (
    HttpError,
    HttpResponse,
    ModelError,
    ModelFinishReason,
    ModelMessage,
    ModelRequest,
    ModelRole,
)
from nexus_ai.agents.models.openai_compatible import OpenAiCompatibleModelProvider
from nexus_ai.integrations.credentials import CredentialType, SecretMaterial

pytestmark = pytest.mark.anyio

_SECRET = SecretMaterial(CredentialType.PROVIDER_SECRET_SET, {"api_key": "sk-test"})


class _FakeTransport:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.seen: dict[str, Any] = {}

    async def request(self, **kw: Any) -> HttpResponse:
        self.seen = kw
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response


def _req(**kw: Any) -> ModelRequest:
    kw.setdefault("provider_api_base", "https://api.vendor.example")
    kw.setdefault("messages", (ModelMessage(role=ModelRole.USER, content="hi"),))
    return ModelRequest(model="gpt-x", timeout_seconds=10.0, **kw)


def _ok(body: dict[str, Any]) -> HttpResponse:
    return HttpResponse(200, {"content-type": "application/json"}, json.dumps(body).encode())


async def test_normal_completion_maps_to_a_neutral_response() -> None:
    transport = _FakeTransport(
        _ok(
            {
                "id": "chatcmpl-1",
                "choices": [{"message": {"content": "Hello!"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            }
        )
    )
    result = await OpenAiCompatibleModelProvider().generate(_req(), _SECRET, transport)
    assert result.assistant_content == "Hello!"
    assert result.finish_reason is ModelFinishReason.STOP
    assert result.usage.input_tokens == 12 and result.usage.output_tokens == 3
    assert result.provider_request_id == "chatcmpl-1"
    # the credential rode only in the Authorization header, to the account's api_base
    assert transport.seen["url"] == "https://api.vendor.example/v1/chat/completions"
    assert transport.seen["headers"]["Authorization"] == "Bearer sk-test"


async def test_tool_call_is_parsed_with_bounded_json_arguments() -> None:
    transport = _FakeTransport(
        _ok(
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {
                                        "name": "crm.get",
                                        "arguments": '{"id": "c1"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            }
        )
    )
    result = await OpenAiCompatibleModelProvider().generate(_req(), _SECRET, transport)
    assert result.finish_reason is ModelFinishReason.TOOL_CALLS
    assert result.tool_calls[0].name == "crm.get"
    assert result.tool_calls[0].arguments == {"id": "c1"}


async def test_unknown_finish_reason_is_a_model_error() -> None:
    transport = _FakeTransport(
        _ok({"choices": [{"message": {"content": "x"}, "finish_reason": "explode"}]})
    )
    with pytest.raises(ModelError):
        await OpenAiCompatibleModelProvider().generate(_req(), _SECRET, transport)


async def test_provider_4xx_becomes_a_model_error_with_a_safe_code() -> None:
    transport = _FakeTransport(
        HttpResponse(
            429,
            {},
            json.dumps({"error": {"code": "rate_limit_exceeded", "message": "slow down"}}).encode(),
        )
    )
    with pytest.raises(ModelError) as excinfo:
        await OpenAiCompatibleModelProvider().generate(_req(), _SECRET, transport)
    assert excinfo.value.status_code == 429
    assert excinfo.value.provider_code == "rate_limit_exceeded"
    assert "slow down" not in str(excinfo.value)  # the provider body never leaks


async def test_transport_timeout_becomes_a_model_error() -> None:
    transport = _FakeTransport(HttpError("timed out", timeout=True))
    with pytest.raises(ModelError) as excinfo:
        await OpenAiCompatibleModelProvider().generate(_req(), _SECRET, transport)
    assert excinfo.value.timeout is True


async def test_non_json_body_is_a_model_error() -> None:
    transport = _FakeTransport(HttpResponse(200, {}, b"<html>gateway</html>"))
    with pytest.raises(ModelError):
        await OpenAiCompatibleModelProvider().generate(_req(), _SECRET, transport)


async def test_a_non_https_api_base_is_refused_before_any_request() -> None:
    transport = _FakeTransport(_ok({"choices": [{"message": {"content": "x"}}]}))
    with pytest.raises(ModelError):
        await OpenAiCompatibleModelProvider().generate(
            _req(provider_api_base="http://insecure.example"), _SECRET, transport
        )
    assert transport.seen == {}  # nothing was sent


async def test_a_missing_credential_is_refused() -> None:
    transport = _FakeTransport(_ok({"choices": [{"message": {"content": "x"}}]}))
    with pytest.raises(ModelError):
        await OpenAiCompatibleModelProvider().generate(_req(), object(), transport)


async def test_credential_without_api_key_field_is_refused() -> None:
    secret = SecretMaterial(CredentialType.PROVIDER_SECRET_SET, {"other": "v"})
    transport = _FakeTransport(_ok({"choices": [{"message": {"content": "x"}}]}))
    with pytest.raises(ModelError):
        await OpenAiCompatibleModelProvider().generate(_req(), secret, transport)


async def test_serialisation_carries_tools_temperature_stop_and_assistant_tool_turn() -> None:
    from nexus_ai.agents.models.base import ModelToolCall, ModelToolSpec

    transport = _FakeTransport(
        _ok({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
    )
    request = _req(
        temperature=0.4,
        max_output_tokens=256,
        stop=("STOP",),
        tools=(ModelToolSpec(name="crm.get", description="d", parameters={"type": "object"}),),
        messages=(
            ModelMessage(role=ModelRole.SYSTEM, content="policy"),
            ModelMessage(
                role=ModelRole.ASSISTANT,
                content="",
                tool_calls=(ModelToolCall(id="c1", name="crm.get", arguments={"id": "1"}),),
            ),
            ModelMessage(role=ModelRole.TOOL, content='{"ok":true}', tool_call_id="c1"),
        ),
    )
    await OpenAiCompatibleModelProvider().generate(request, _SECRET, transport)
    sent = json.loads(transport.seen["body"])
    assert sent["temperature"] == 0.4 and sent["max_tokens"] == 256 and sent["stop"] == ["STOP"]
    assert sent["tools"][0]["function"]["name"] == "crm.get"
    roles = [m["role"] for m in sent["messages"]]
    assert roles == ["system", "assistant", "tool"]
    assert sent["messages"][1]["tool_calls"][0]["function"]["name"] == "crm.get"


@pytest.mark.parametrize(
    "body",
    [
        {"choices": []},
        {"choices": [42]},
        {"choices": [{"message": "not-a-dict"}]},
        {"choices": [{"message": {"tool_calls": "nope"}}]},
        {"choices": [{"message": {"tool_calls": ["not-a-dict"]}}]},
    ],
)
async def test_malformed_choice_shapes_are_model_errors(body: dict[str, Any]) -> None:
    with pytest.raises(ModelError):
        await OpenAiCompatibleModelProvider().generate(_req(), _SECRET, _FakeTransport(_ok(body)))


async def test_tool_call_with_unparseable_arguments_is_a_model_error() -> None:
    body = {
        "choices": [
            {
                "message": {
                    "tool_calls": [{"id": "c1", "function": {"name": "t", "arguments": "{bad"}}]
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    with pytest.raises(ModelError):
        await OpenAiCompatibleModelProvider().generate(_req(), _SECRET, _FakeTransport(_ok(body)))


async def test_stream_yields_a_single_terminal_chunk() -> None:
    transport = _FakeTransport(
        _ok(
            {
                "choices": [{"message": {"content": "streamed"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            }
        )
    )
    chunks = [
        chunk async for chunk in OpenAiCompatibleModelProvider().stream(_req(), _SECRET, transport)
    ]
    assert len(chunks) == 1
    assert chunks[0].delta_text == "streamed"
    assert chunks[0].finish_reason is ModelFinishReason.STOP


def test_registry_resolves_known_providers_and_rejects_unknown() -> None:
    from nexus_ai.agents.models.fake import FakeModelProvider
    from nexus_ai.agents.models.openai_compatible import (
        OpenAiCompatibleModelProvider as _OAI,
    )
    from nexus_ai.agents.models.registry import known_model_providers, resolve_model_provider

    assert isinstance(resolve_model_provider("fake"), FakeModelProvider)
    assert isinstance(resolve_model_provider("openai_compatible"), _OAI)
    assert known_model_providers() == ("fake", "openai_compatible")
    with pytest.raises(LookupError):
        resolve_model_provider("some_vendor")
