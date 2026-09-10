"""An OpenAI-compatible chat-completions model adapter (NXS-P13, ADR-0091).

Speaks the widely-implemented ``POST {api_base}/v1/chat/completions`` contract (OpenAI,
Azure OpenAI, many local servers, other OpenAI-compatible gateways) behind the
Nexus-neutral :class:`ModelProviderAdapter`. The REST origin is the model provider
account's validated ``api_base`` — SSRF-checked at registration and again by the governed
transport; the model never influences it. The API key lives in the NXS-P07 vault and is
attached only at this boundary — it never reaches a table, a log, an event or the model.

CONTRACT-CERTIFIED against the documented response shape; not LIVE-PROVIDER-CERTIFIED
(no live credential is exercised in CI).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from nexus_ai.agents.models.base import (
    HttpError,
    ModelError,
    ModelFinishReason,
    ModelHttpTransport,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelStreamChunk,
    ModelToolCall,
    ModelUsage,
)
from nexus_ai.integrations.credentials import SecretMaterial

_FINISH_MAP: dict[str, ModelFinishReason] = {
    "stop": ModelFinishReason.STOP,
    "tool_calls": ModelFinishReason.TOOL_CALLS,
    "function_call": ModelFinishReason.TOOL_CALLS,
    "length": ModelFinishReason.LENGTH,
    "content_filter": ModelFinishReason.CONTENT_FILTER,
}


class OpenAiCompatibleModelProvider:
    key = "openai_compatible"

    async def generate(
        self, request: ModelRequest, secret: Any, http: ModelHttpTransport
    ) -> ModelResponse:
        api_key = _api_key(secret)
        api_base = (request.provider_api_base or "").rstrip("/")
        if not api_base.startswith("https://"):
            raise ModelError(
                "the model API base is not configured as an https origin "
                "(it comes from the provider account, never a request or the model)"
            )
        url = f"{api_base}/v1/chat/completions"
        payload = _serialize(request)
        try:
            response = await http.request(
                method="POST",
                url=url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                timeout_seconds=request.timeout_seconds,
            )
        except HttpError as exc:
            raise ModelError("the model provider request failed", timeout=exc.timeout) from exc
        if response.status_code >= 400:
            raise ModelError(
                "the model provider rejected the request",
                status_code=response.status_code,
                provider_code=_safe_error_code(response.body),
            )
        try:
            body = json.loads(response.body or b"{}")
        except (json.JSONDecodeError, ValueError) as exc:
            raise ModelError("the model provider returned a non-JSON body") from exc
        return _parse(body)

    async def stream(
        self, request: ModelRequest, secret: Any, http: ModelHttpTransport
    ) -> AsyncIterator[ModelStreamChunk]:
        # Server-Sent Events streaming is a later increment — the governed transport is
        # request/response today. A single terminal chunk keeps the contract honest.
        result = await self.generate(request, secret, http)
        yield ModelStreamChunk(
            delta_text=result.assistant_content,
            tool_calls=result.tool_calls,
            finish_reason=result.finish_reason,
            usage=result.usage,
        )


def _api_key(secret: Any) -> str:
    if not isinstance(secret, SecretMaterial):
        raise ModelError("no model provider credential is available")
    from nexus_ai.integrations.errors import IntegrationCredentialUnavailableError

    try:
        return secret.field("api_key")
    except IntegrationCredentialUnavailableError as exc:
        raise ModelError("the model provider credential has no api_key field") from exc


def _serialize(request: ModelRequest) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    for message in request.messages:
        messages.append(_serialize_message(message))
    payload: dict[str, Any] = {"model": request.model, "messages": messages}
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.max_output_tokens is not None:
        payload["max_tokens"] = request.max_output_tokens
    if request.stop:
        payload["stop"] = list(request.stop)
    if request.tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in request.tools
        ]
    return payload


def _serialize_message(message: ModelMessage) -> dict[str, Any]:
    if message.role is ModelRole.TOOL:
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id or "",
            "content": message.content,
        }
    if message.role is ModelRole.ASSISTANT and message.tool_calls:
        return {
            "role": "assistant",
            "content": message.content or None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, separators=(",", ":")),
                    },
                }
                for call in message.tool_calls
            ],
        }
    return {"role": message.role.value, "content": message.content}


def _parse(body: dict[str, Any]) -> ModelResponse:
    choices = body.get("choices") or []
    if not isinstance(choices, list) or not choices:
        raise ModelError("the model provider returned no choices")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ModelError("the model provider returned a malformed choice")
    message = choice.get("message") or {}
    if not isinstance(message, dict):
        raise ModelError("the model provider returned a malformed message")
    content = message.get("content")
    assistant_content = content if isinstance(content, str) else ""
    tool_calls = _parse_tool_calls(message.get("tool_calls"))
    finish_raw = str(choice.get("finish_reason") or "stop")
    finish = _FINISH_MAP.get(finish_raw)
    if finish is None:
        raise ModelError(f"the model provider returned an unknown finish reason {finish_raw!r}")
    usage_raw = body.get("usage") or {}
    usage = ModelUsage(
        input_tokens=_int(usage_raw.get("prompt_tokens")),
        output_tokens=_int(usage_raw.get("completion_tokens")),
    )
    request_id = body.get("id")
    return ModelResponse(
        assistant_content=assistant_content,
        tool_calls=tool_calls,
        finish_reason=finish,
        usage=usage,
        provider_request_id=request_id if isinstance(request_id, str) else None,
    )


def _parse_tool_calls(raw: Any) -> tuple[ModelToolCall, ...]:
    if not raw:
        return ()
    if not isinstance(raw, list):
        raise ModelError("the model provider returned a malformed tool_calls array")
    calls: list[ModelToolCall] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ModelError("the model provider returned a malformed tool call")
        function = entry.get("function") or {}
        name = function.get("name")
        arguments_raw = function.get("arguments")
        if not isinstance(name, str) or not isinstance(arguments_raw, str):
            raise ModelError("a model tool call is missing a name or arguments")
        try:
            arguments = json.loads(arguments_raw) if arguments_raw else {}
        except (json.JSONDecodeError, ValueError) as exc:
            raise ModelError("a model tool call has unparseable arguments") from exc
        if not isinstance(arguments, dict):
            raise ModelError("a model tool call's arguments are not a JSON object")
        call_id = entry.get("id")
        calls.append(
            ModelToolCall(
                id=call_id if isinstance(call_id, str) and call_id else f"call-{len(calls)}",
                name=name,
                arguments=arguments,
            )
        )
    return tuple(calls)


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _safe_error_code(body: bytes) -> str | None:
    try:
        parsed = json.loads(body or b"{}")
        error = parsed.get("error") if isinstance(parsed, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        return code if isinstance(code, str) and len(code) <= 64 else None
    except json.JSONDecodeError, ValueError, AttributeError:
        return None
