"""The agent -> NXS-P08 Tool Engine bridge (NXS-P13, ADR-0093).

The ONLY path from a model tool request to external action. A model requesting a tool is
NOT authorization: this bridge (a) enforces the agent's server-side tool allow-list, (b)
validates the tool-call structure (bounded name / arguments / nesting depth), (c) hands a
governed :class:`~nexus_ai.tools.entities.ToolInvocation` to
:meth:`~nexus_ai.tools.service.ToolEngine.invoke` — which runs the full P08 policy
pipeline (RBAC, org/tool policy, risk ceiling, argument schema, durable idempotency,
governed NXS-P07 execution, output-schema validation), and (d) bounds the result before
it is re-injected into the model context. The agent runtime never calls an integration
adapter, reads a credential, executes SQL / a shell, or opens a network connection.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from nexus_ai.agents.errors import AgentToolInvalidError
from nexus_ai.agents.idempotency import arguments_hash
from nexus_ai.agents.models.base import ModelToolCall
from nexus_ai.core.config import AgentRuntimeSettings
from nexus_ai.core.errors import NxsError
from nexus_ai.domain.auth.entities import Principal
from nexus_ai.tools.entities import ToolInvocation, ToolResult
from nexus_ai.tools.errors import (
    ToolNotFoundError,
    ToolPermissionDeniedError,
    ToolPolicyDeniedError,
)
from nexus_ai.tools.service import ToolEngine

_DENY_ERRORS = (ToolPermissionDeniedError, ToolPolicyDeniedError, ToolNotFoundError)


@dataclass(frozen=True, slots=True)
class ToolCallOutcome:
    tool_key: str
    arguments_hash: str
    allowed: bool
    ok: bool
    denied_reason: str | None
    result_class: str | None
    status_code: int | None
    error_code: str | None
    latency_ms: int
    #: the bounded, safe payload re-injected into the model context (never raw secrets)
    reinjected: dict[str, Any]


class AgentToolBridge:
    def __init__(self, engine: ToolEngine, settings: AgentRuntimeSettings) -> None:
        self._engine = engine
        self._cfg = settings

    def validate_structure(self, call: ModelToolCall) -> None:
        """Fail closed on a structurally invalid model tool call — BEFORE any authority
        or execution decision."""
        if not call.name or len(call.name) > 96:
            raise AgentToolInvalidError("a model tool call has an out-of-bounds name")
        if not isinstance(call.arguments, dict):
            raise AgentToolInvalidError("a model tool call's arguments are not an object")
        if _json_depth(call.arguments) > self._cfg.max_tool_argument_depth:
            raise AgentToolInvalidError(
                "a model tool call's arguments nest deeper than the configured limit"
            )
        canonical = _canonical_bytes(call.arguments)
        if canonical > self._cfg.max_tool_arguments_bytes:
            raise AgentToolInvalidError("a model tool call's arguments exceed the size limit")

    async def execute(
        self,
        *,
        principal: Principal,
        allow_list: frozenset[str],
        call: ModelToolCall,
        correlation_id: str | None,
        idempotency_seed: str,
    ) -> ToolCallOutcome:
        self.validate_structure(call)
        arg_hash = arguments_hash(call.arguments)

        if call.name not in allow_list:
            # a model can never widen its own authority — not on the agent's list.
            return ToolCallOutcome(
                tool_key=call.name,
                arguments_hash=arg_hash,
                allowed=False,
                ok=False,
                denied_reason="not_on_agent_allow_list",
                result_class=None,
                status_code=None,
                error_code="NXS_AGENT_TOOL_DENIED",
                latency_ms=0,
                reinjected=self._reinject({"error": "tool_not_available", "tool": call.name[:64]}),
            )

        # The P08 idempotency key is a canonical SEMANTIC execution identity:
        # (turn seed, tool_key, canonical-argument hash). It deliberately excludes the
        # model-generated tool_call_id and the loop iteration — the identical semantic
        # tool request anywhere in one AgentTurn resolves to ONE external effect.
        invocation = ToolInvocation(
            tool_key=call.name,
            arguments=call.arguments,
            idempotency_key=_derive_key(idempotency_seed, call.name, arg_hash),
            correlation_id=correlation_id,
        )
        started = time.monotonic()
        try:
            result = await self._engine.invoke(principal, invocation)
        except _DENY_ERRORS as exc:
            latency = int((time.monotonic() - started) * 1000)
            return ToolCallOutcome(
                tool_key=call.name,
                arguments_hash=arg_hash,
                allowed=False,
                ok=False,
                denied_reason=exc.code,
                result_class=None,
                status_code=exc.status,
                error_code=exc.code,
                latency_ms=latency,
                reinjected=self._reinject({"error": "tool_denied", "reason": exc.code}),
            )
        except NxsError as exc:
            latency = int((time.monotonic() - started) * 1000)
            return ToolCallOutcome(
                tool_key=call.name,
                arguments_hash=arg_hash,
                allowed=True,
                ok=False,
                denied_reason=None,
                result_class="ERROR",
                status_code=exc.status,
                error_code=exc.code,
                latency_ms=latency,
                reinjected=self._reinject({"error": "tool_failed", "code": exc.code}),
            )

        latency = int((time.monotonic() - started) * 1000)
        return ToolCallOutcome(
            tool_key=call.name,
            arguments_hash=arg_hash,
            allowed=True,
            ok=result.ok,
            denied_reason=None,
            result_class=result.result_class.value,
            status_code=result.status_code,
            error_code=result.error_code,
            latency_ms=latency or result.duration_ms,
            reinjected=self._bound_result(result),
        )

    def _reinject(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Every re-injected object is bounded so its serialisation into the model
        context cannot exceed ``max_tool_result_bytes`` — for ASCII or arbitrary
        Unicode, measured with the widest serialisation and never split mid code point."""
        return bound_reinjection(payload, self._cfg.max_tool_result_bytes)

    def _bound_result(self, result: ToolResult) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": result.ok,
            "status_code": result.status_code,
            "result_class": result.result_class.value,
        }
        if result.error_code is not None:
            payload["error_code"] = result.error_code
        if result.output is not None:
            payload["output"] = result.output
        return self._reinject(payload)


def _derive_key(seed: str, tool_key: str, arg_hash: str) -> str:
    raw = f"{seed}:{tool_key}:{arg_hash}"
    import hashlib

    return "agt-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _json_depth(value: Any, current: int = 1) -> int:
    if isinstance(value, dict):
        return max((_json_depth(v, current + 1) for v in value.values()), default=current)
    if isinstance(value, list):
        return max((_json_depth(v, current + 1) for v in value), default=current)
    return current


def _canonical_bytes(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def reinjection_json(payload: dict[str, Any]) -> str:
    """The single canonical serialisation of a re-injected tool result — the exact form
    the runtime writes into the model context (compact separators, ASCII-escaped)."""
    return json.dumps(payload, separators=(",", ":"), default=str)


def _widest_bytes(value: Any) -> int:
    """UTF-8 byte length of the widest single-line JSON serialisation of ``value``:
    ASCII-escaped (``\\uXXXX`` is never shorter than the raw code point) and the default
    ``", "`` / ``": "`` separators (never shorter than the compact ``,`` / ``:`` the
    runtime uses). Bounding this bounds the bytes the runtime actually injects and any
    plain ``json.dumps`` an auditor might measure with."""
    return len(json.dumps(value, default=str).encode("utf-8"))


def bound_reinjection(payload: dict[str, Any], limit: int) -> dict[str, Any]:
    """Return a re-injection object whose JSON serialisation is <= ``limit`` UTF-8 bytes
    for ANY serialisation. Only the ``output`` value can be large; it is replaced with a
    ``{"truncated": true, "preview": ...}`` object trimmed on whole code points so the
    FINAL object (wrapper included) fits. Falls back to a minimal truthful envelope."""
    if _widest_bytes(payload) <= limit:
        return payload
    reserved = {key: value for key, value in payload.items() if key != "output"}
    if "output" in payload:
        skeleton_bytes = _widest_bytes({**reserved, "output": None})
        value_budget = limit - skeleton_bytes + len("null")
        candidate = {**reserved, "output": _fit_value(payload["output"], value_budget)}
        if _widest_bytes(candidate) <= limit:
            return candidate
    minimal = {"truncated": True, "ok": bool(payload.get("ok", False))}
    return minimal if _widest_bytes(minimal) <= limit else {"truncated": True}


def _fit_value(value: Any, budget: int) -> Any:
    if budget > 0 and _widest_bytes(value) <= budget:
        return value
    # A string preview is a prefix of the string itself; anything else previews its
    # compact JSON. Either way the cut is on a whole code point (Python str slicing).
    source = (
        value
        if isinstance(value, str)
        else json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)
    )
    base: dict[str, Any] = {"truncated": True}
    if budget <= 0 or _widest_bytes({**base, "preview": ""}) > budget:
        return base
    low, high = 0, len(source)
    while low < high:
        mid = (low + high + 1) // 2
        if _widest_bytes({**base, "preview": source[:mid]}) <= budget:
            low = mid
        else:
            high = mid - 1
    return {**base, "preview": source[:low]}
