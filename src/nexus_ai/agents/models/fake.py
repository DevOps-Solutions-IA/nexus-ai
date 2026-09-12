"""A deterministic fake model provider (NXS-P13, ADR-0091 §24).

Implements the same :class:`ModelProviderAdapter` interface as every production adapter.
Behaviour is scripted per model call so CI can exercise: a normal answer, a streamed
answer, one or many tool requests, a malformed / too-deep tool argument, an unknown
tool, a provider timeout / disconnect / rate-limit, a malformed response, a hang (for
cancellation) and a same-tool loop attempt. No network, no vendor shape.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from nexus_ai.agents.models.base import (
    HttpError,
    ModelError,
    ModelFinishReason,
    ModelHttpTransport,
    ModelRequest,
    ModelResponse,
    ModelStreamChunk,
    ModelToolCall,
    ModelUsage,
)


def _deep(depth: int) -> dict[str, Any]:
    node: dict[str, Any] = {"leaf": 1}
    for _ in range(depth):
        node = {"nested": node}
    return node


@dataclass(slots=True)
class FakeModelTurn:
    """What the fake returns on one model call."""

    content: str = ""
    tool_calls: tuple[tuple[str, dict[str, Any]], ...] = ()
    finish_reason: ModelFinishReason | None = None
    input_tokens: int = 11
    output_tokens: int = 7
    #: adversarial knobs
    raise_timeout: bool = False
    raise_connect: bool = False
    raise_status: int | None = None
    unknown_finish: bool = False
    oversized_chars: int = 0
    deep_tool_args: int = 0
    duplicate_tool_ids: bool = False
    #: prefix for the model-generated tool_call_id — vary it across scripted turns to
    #: prove semantic (not id-based) deduplication of repeated tool requests.
    tool_call_id_prefix: str = "call"
    hang_seconds: float = 0.0
    provider_request_id: str | None = "fake-req"


@dataclass(slots=True)
class FakeModelProvider:
    """A stateful, test-scoped fake. In production the registry hands back a stateless
    instance whose default behaviour is a bounded echo."""

    key: str = "fake"
    script: list[FakeModelTurn] = field(default_factory=list)
    calls: list[ModelRequest] = field(default_factory=list)

    def _next(self, request: ModelRequest) -> FakeModelTurn:
        self.calls.append(request)
        if self.script:
            return self.script.pop(0)
        last_user = next(
            (m.content for m in reversed(request.messages) if m.role.value == "user"), ""
        )
        return FakeModelTurn(content=f"echo: {last_user[:200]}")

    async def generate(
        self, request: ModelRequest, secret: Any, http: ModelHttpTransport
    ) -> ModelResponse:
        turn = self._next(request)
        await self._maybe_fail(turn)
        return self._build(turn)

    async def stream(
        self, request: ModelRequest, secret: Any, http: ModelHttpTransport
    ) -> AsyncIterator[ModelStreamChunk]:
        turn = self._next(request)
        await self._maybe_fail(turn)
        response = self._build(turn)
        text = response.assistant_content
        for i in range(0, len(text), 8):
            yield ModelStreamChunk(delta_text=text[i : i + 8])
        yield ModelStreamChunk(
            tool_calls=response.tool_calls,
            finish_reason=response.finish_reason,
            usage=response.usage,
        )

    async def _maybe_fail(self, turn: FakeModelTurn) -> None:
        if turn.hang_seconds > 0:
            await asyncio.sleep(turn.hang_seconds)
        if turn.raise_timeout:
            raise HttpError("fake provider timed out", timeout=True)
        if turn.raise_connect:
            raise HttpError("fake provider connection reset", connect=True)
        if turn.raise_status is not None:
            raise ModelError(
                "fake provider returned an error status",
                status_code=turn.raise_status,
                provider_code=f"fake_{turn.raise_status}",
            )

    def _build(self, turn: FakeModelTurn) -> ModelResponse:
        if turn.unknown_finish:
            raise ModelError("fake provider returned an unknown finish reason")
        content = turn.content
        if turn.oversized_chars:
            content = "x" * turn.oversized_chars
        calls: list[ModelToolCall] = []
        for idx, (name, args) in enumerate(turn.tool_calls):
            call_args = _deep(turn.deep_tool_args) if turn.deep_tool_args else dict(args)
            call_id = "dup" if turn.duplicate_tool_ids else f"{turn.tool_call_id_prefix}-{idx}"
            calls.append(ModelToolCall(id=call_id, name=name, arguments=call_args))
        finish = turn.finish_reason or (
            ModelFinishReason.TOOL_CALLS if calls else ModelFinishReason.STOP
        )
        return ModelResponse(
            assistant_content=content,
            tool_calls=tuple(calls),
            finish_reason=finish,
            usage=ModelUsage(input_tokens=turn.input_tokens, output_tokens=turn.output_tokens),
            provider_request_id=turn.provider_request_id,
        )
