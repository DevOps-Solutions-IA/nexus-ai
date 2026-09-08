# ADR-0068: Tool timeout enforcement at the engine boundary

Status: Accepted. Corrects a gap in ADR-0061 / ADR-0062 / ADR-0067: `ToolDefinition.timeout_seconds` was a declared, versioned attribute but `ToolEngine.invoke` never enforced it — the field was dead metadata and only the NXS-P07 operation/executor timeout applied. Context: NXS-TOOL-001 requires that a declared tool timeout policy is real, deterministic, and does not bypass the governed Integration Hub path.

Decision:

**The tool timeout is an outer upper bound around the governed P07 execution.** In `ToolEngine._execute_bounded`:
- `tool.timeout_seconds is None` → normal governed P07 behaviour; the Integration Hub's own timeout / retry / circuit / rate-limit controls are the only bound.
- `tool.timeout_seconds` set → the Tool Engine wraps the single `IntegrationHubService.execute` call in `asyncio.timeout(tool.timeout_seconds)` (the canonical async timeout primitive). P07's controls stay fully active inside that scope; **the stricter of the two limits fires first**. The Tool Engine still opens no external connection of its own and never disables or bypasses any P07 control.

**Deterministic expiry mapping.** Expiry of the tool bound raises `ToolTimeoutError` (`NXS_TOOL_TIMEOUT`, HTTP 504, retryable) with `extensions.timeout_scope = "tool"` and `extensions.timeout_seconds`. A P07 operation timeout that fires first is surfaced through the same code with `timeout_scope = "integration"` and the original `downstream_code` (`NXS_INT_TIMEOUT`). Either way `_finish_failure` writes a `tool_execution_records` receipt with `result_class = TIMEOUT`, `error_code = NXS_TOOL_TIMEOUT`, a real `duration_ms`, and emits a `tools.invocation.failed` event. No request body, header, base URL or secret ever reaches the receipt or the event.

**Idempotency stays deterministic on timeout.** A tool timeout propagates as an `NxsError`, so `ToolEngine.invoke` finalises an owned idempotency claim as `FAILED` (never left `PENDING`, never `COMPLETED`). A subsequent call with the same key + same fingerprint then follows the documented failed-invocation rule (`ToolExecutionFailedError`, "use a new key") and does not re-run the external call. Exactly-once is not claimed.

**Versioning.** `ToolRegistry.update` now bumps `ToolDefinition.version` when `timeout_seconds` changes, because the timeout is execution policy — the effective behaviour must never change without the deterministic version changing (ADR-0062).

Consequences: a tool's declared timeout is now an enforced contract; an operator can bound a slow or unreliable integration per tool independently of the integration's own operation timeout; the regression suite (`tests/resilience/test_tool_resilience.py`) distinguishes the P08 bound from the P07 bound by elapsed time and `timeout_scope`.
