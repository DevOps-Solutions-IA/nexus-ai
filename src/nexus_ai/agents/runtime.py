"""The bounded agent turn loop (NXS-P13, ADR-0094).

``AgentRuntime.run_turn`` drives one reasoning turn: assemble the provider-neutral
request, call the model (bounded timeout), validate the untrusted response, and — while
the model wants tools and the budgets allow — run each requested tool through the
:class:`~nexus_ai.agents.toolbridge.AgentToolBridge` and re-inject the bounded result.

Turn-wide invariants held across EVERY model iteration:
  * the same semantic tool call (tool key + canonical-argument hash) reappearing anywhere
    in the turn reuses the first outcome — the Tool Engine runs once, the P08 idempotency
    key never changes, no repeated external side effect;
  * every model-requested call counts toward ``max_tool_calls_per_turn`` (duplicates
    included); the call that would exceed it fails the turn before it reaches the engine.

The loop terminates deterministically: a final answer, the tool-iteration ceiling, the
per-turn tool-call budget, a provider error, or cancellation. It performs no database
work (the caller owns persistence and the terminal transition), so a cancelled turn
never leaks a connection.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from nexus_ai.agents.context import AssembledContext
from nexus_ai.agents.errors import (
    AgentOutputInvalidError,
    AgentProviderError,
    AgentProviderTimeoutError,
    AgentToolLoopLimitError,
)
from nexus_ai.agents.idempotency import arguments_hash
from nexus_ai.agents.models.base import (
    HttpError,
    ModelError,
    ModelFinishReason,
    ModelHttpTransport,
    ModelMessage,
    ModelProviderAdapter,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelToolSpec,
    ModelUsage,
)
from nexus_ai.agents.prompt import build_messages
from nexus_ai.agents.state_machine import AgentSessionDisposition
from nexus_ai.agents.toolbridge import AgentToolBridge, ToolCallOutcome, reinjection_json
from nexus_ai.core.config import AgentRuntimeSettings
from nexus_ai.core.logging import get_logger
from nexus_ai.domain.auth.entities import Principal

ToolCallback = Callable[[str, ToolCallOutcome, int], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class TurnContext:
    model: str
    provider_api_base: str
    agent_instructions: str
    temperature: float | None
    max_output_tokens: int | None
    tool_specs: tuple[ModelToolSpec, ...]
    allow_list: frozenset[str]
    max_tool_iterations: int
    principal: Principal
    correlation_id: str | None
    idempotency_seed: str
    context: AssembledContext
    channel: str


@dataclass(slots=True)
class TurnOutcome:
    disposition: AgentSessionDisposition
    content: str = ""
    finish_reason: str = "STOP"
    usage: ModelUsage = field(default_factory=ModelUsage)
    tool_iterations: int = 0
    tool_calls: list[ToolCallOutcome] = field(default_factory=list)
    error_code: str | None = None


class AgentRuntime:
    def __init__(self, settings: AgentRuntimeSettings, tool_bridge: AgentToolBridge) -> None:
        self._cfg = settings
        self._tools = tool_bridge
        self._log = get_logger("nexus_ai.agents.runtime")

    async def run_turn(
        self,
        *,
        ctx: TurnContext,
        adapter: ModelProviderAdapter,
        secret: Any,
        http: ModelHttpTransport,
        on_tool: ToolCallback,
    ) -> TurnOutcome:
        messages = build_messages(
            agent_instructions=ctx.agent_instructions,
            context=ctx.context,
            max_output_chars=self._cfg.max_input_chars,
        )
        usage_total = ModelUsage()
        outcomes: list[ToolCallOutcome] = []
        # Turn-scoped, spanning EVERY model iteration:
        #  * `executed` — the same semantic call (tool_key + canonical-argument hash)
        #    reappearing anywhere in the turn reuses the first ToolCallOutcome; the Tool
        #    Engine is never invoked a second time and the P08 idempotency key never
        #    changes. The model-generated tool_call_id is used only to correlate the
        #    protocol response, never to defeat this deduplication.
        #  * `tool_calls_requested` — every model-requested call counts toward the
        #    turn-wide budget, duplicates included (a re-request is loop behaviour).
        executed: dict[tuple[str, str], ToolCallOutcome] = {}
        tool_calls_requested = 0

        for iteration in range(ctx.max_tool_iterations + 1):
            response = await self._call_model(ctx, adapter, secret, http, messages)
            self._validate_response(response)
            usage_total = ModelUsage(
                usage_total.input_tokens + response.usage.input_tokens,
                usage_total.output_tokens + response.usage.output_tokens,
            )

            wants_tools = bool(response.tool_calls) and (
                response.finish_reason is ModelFinishReason.TOOL_CALLS
            )
            if not wants_tools:
                return TurnOutcome(
                    disposition=AgentSessionDisposition.COMPLETED,
                    content=response.assistant_content,
                    finish_reason=response.finish_reason.value,
                    usage=usage_total,
                    tool_iterations=iteration,
                    tool_calls=outcomes,
                )

            if iteration >= ctx.max_tool_iterations:
                raise AgentToolLoopLimitError(
                    "the model kept requesting tools past the iteration ceiling"
                )
            if len(response.tool_calls) > self._cfg.max_tool_calls_per_response:
                raise AgentOutputInvalidError(
                    "the model requested more tools in one response than permitted"
                )

            messages.append(
                ModelMessage(
                    role=ModelRole.ASSISTANT,
                    content=response.assistant_content,
                    tool_calls=response.tool_calls,
                )
            )
            for call in response.tool_calls:
                tool_calls_requested += 1
                if tool_calls_requested > self._cfg.max_tool_calls_per_turn:
                    raise AgentToolLoopLimitError("the turn exhausted its total tool-call budget")
                key = (call.name, arguments_hash(call.arguments))
                outcome = executed.get(key)
                if outcome is None:
                    outcome = await self._tools.execute(
                        principal=ctx.principal,
                        allow_list=ctx.allow_list,
                        call=call,
                        correlation_id=ctx.correlation_id,
                        idempotency_seed=ctx.idempotency_seed,
                    )
                    executed[key] = outcome
                    outcomes.append(outcome)
                    await on_tool(call.id, outcome, iteration)
                messages.append(
                    ModelMessage(
                        role=ModelRole.TOOL,
                        content=reinjection_json(outcome.reinjected),
                        tool_call_id=call.id,
                    )
                )

        raise AgentToolLoopLimitError("the bounded tool loop did not converge")

    async def _call_model(
        self,
        ctx: TurnContext,
        adapter: ModelProviderAdapter,
        secret: Any,
        http: ModelHttpTransport,
        messages: list[ModelMessage],
    ) -> ModelResponse:
        request = ModelRequest(
            model=ctx.model,
            messages=tuple(messages),
            tools=ctx.tool_specs,
            temperature=ctx.temperature,
            max_output_tokens=ctx.max_output_tokens,
            timeout_seconds=self._cfg.model_response_timeout_seconds,
            provider_api_base=ctx.provider_api_base,
            correlation_id=ctx.correlation_id,
        )
        try:
            async with asyncio.timeout(self._cfg.model_response_timeout_seconds):
                return await adapter.generate(request, secret, http)
        except TimeoutError as exc:
            raise AgentProviderTimeoutError(
                "the model provider call exceeded its deadline"
            ) from exc
        except HttpError as exc:
            if exc.timeout:
                raise AgentProviderTimeoutError("the model provider call timed out") from exc
            raise AgentProviderError(
                "the model provider connection failed", retryable=True
            ) from exc
        except ModelError as exc:
            if exc.timeout:
                raise AgentProviderTimeoutError("the model provider call timed out") from exc
            from nexus_ai.agents.errors import provider_rest_failure

            if exc.status_code is not None:
                raise provider_rest_failure(
                    "the model provider returned an error",
                    status_code=exc.status_code,
                    provider_code=exc.provider_code,
                ) from exc
            raise AgentOutputInvalidError(str(exc.detail)) from exc

    def _validate_response(self, response: ModelResponse) -> None:
        if len(response.assistant_content) > self._cfg.max_output_chars:
            raise AgentOutputInvalidError("the model returned an assistant block over the limit")
        ids = [c.id for c in response.tool_calls]
        if len(ids) != len(set(ids)):
            raise AgentOutputInvalidError("the model returned duplicate tool-call ids")
        for call in response.tool_calls:
            self._tools.validate_structure(call)
