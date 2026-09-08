# ADR-0066: Tool result and error taxonomy

Status: Accepted. Context: NXS-TOOL-001 requires a normalized `ToolResult` and a stable RFC 9457 / NXS error taxonomy, plus safe handling of untrusted external responses (result poisoning).

Decision:

**One normalized result shape.** `ToolResult` carries `tool_key`, `tool_version`, `result_class`, `ok`, `status_code`, `output`, `error_code`, `error_detail`, `downstream_code`, `downstream_status`, `retry_count`, `duration_ms`, `correlation_id`, `idempotency_key` and `replayed`. `result_class` is a closed `ToolResultClass` enum (`SUCCESS`, `ARGS_INVALID`, `PERMISSION_DENIED`, `POLICY_DENIED`, `DISABLED`, `NOT_FOUND`, `BINDING_INVALID`, `IDEMPOTENCY_CONFLICT`, `IDEMPOTENCY_REQUIRED`, `DOWNSTREAM_ERROR`, `RESULT_INVALID`, `TIMEOUT`, `RATE_LIMITED`, `EXECUTION_FAILED`). It round-trips through `model_dump(mode="json")` / `model_validate` unchanged.

**Stable `NXS_TOOL_*` codes.** Every failure is an `NxsError` subclass with a code prefixed `NXS_TOOL_`, a stable HTTP status and a distinct title. The set is frozen in `TOOL_ERRORS`; a contract test asserts codes are unique, statuses are `400..599`, and the retryable classification is stable: `NXS_TOOL_TIMEOUT`, `NXS_TOOL_RATE_LIMITED` and `NXS_TOOL_EXECUTION_IN_PROGRESS` are retryable; `NXS_TOOL_PERMISSION_DENIED`, `NXS_TOOL_ARGS_INVALID` and the other policy/validation errors are not. All are rendered as RFC 9457 Problem Details by the app-wide `NxsError` handler, so `type` ends in the code.

**Raise vs. surface.** `ToolEngine.invoke` raises the mapped `NxsError` for a failure — the API turns it into a Problem Details response. `ToolResult` with `ok=False` is used where the engine records a handled failure (execution receipt, replay of a stored failure). Either way the `result_class` and `error_code` are the same stable values.

**Untrusted responses are validated, not trusted.** Step 13 runs the Hub output through the tool's `output_schema` (when declared) with `validate_output`; a violation is `NXS_TOOL_RESULT_INVALID` (502) and the poisoned payload never reaches the caller or the event bus. Argument input is validated the same way against the closed `input_schema` (`NXS_TOOL_ARGS_INVALID`, 422), with hard bounds on schema size/depth and argument size to bound a hostile document.

**Downstream mapping.** `map_downstream_error` collapses P07 error codes: timeout codes → `NXS_TOOL_TIMEOUT`; rate-limit codes → `NXS_TOOL_RATE_LIMITED`; everything else → `NXS_TOOL_DOWNSTREAM_ERROR` carrying `downstream_code`, `upstream_status` and the P07 `retryable` flag.

Consequences: a caller (including the future Agent Runtime) can branch on a small closed enum and a stable code set without parsing prose or upstream specifics; audit rows record both the tool-level class and the original downstream code.
