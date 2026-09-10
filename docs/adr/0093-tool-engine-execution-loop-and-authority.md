# ADR-0093: The bounded tool-execution loop and where tool authority lives

Status: Accepted — NXS-P13 (`NXS-AGENT-001`), 2026-09-10.

## Context

The model asks to call tools. If "the model asked" were authorization, a prompt
injection or a confused model would have arbitrary external reach. The loop also must
terminate deterministically and never let a model drive repeated side effects.

## Decision

### The Tool Engine (NXS-P08) is the only external-action path

`AgentToolBridge` (`nexus_ai.agents.toolbridge`) is the single seam between a model tool
request and any external effect. The agent runtime never imports an integration adapter,
never reads a credential, never runs SQL or a shell, never opens a socket — contract-
tested against `toolbridge.py`, `runtime.py` and `service.py` (no `import httpx`, `import
subprocess`, `import os`, `nexus_ai.integrations.adapters`, `IntegrationHubService`,
`sqlalchemy.text`).

### Authority is server-side, not model-side

For every requested call the bridge, in order:

1. **Validates structure** first — bounded name (≤96), arguments must be a JSON object,
   nesting ≤ `max_tool_argument_depth`, canonical size ≤ `max_tool_arguments_bytes`. A
   violation is `AgentToolInvalidError`, before any authority decision.
2. **Enforces the agent allow-list** — `call.name not in agent.tool_keys` returns a
   `DENIED` outcome with `denied_reason="not_on_agent_allow_list"` and **never reaches
   the Tool Engine** (security-tested: `tool_execution_records` stays empty).
3. **Delegates to `ToolEngine.invoke(principal, invocation)`** — which runs the full P08
   pipeline: USER-scoped RBAC (`integration:execute` + `tool:invoke`), org/tool policy,
   risk ceiling, argument-schema validation, durable idempotency, governed NXS-P07
   execution, output-schema validation. `ToolPermissionDeniedError` /
   `ToolPolicyDeniedError` / `ToolNotFoundError` become a `DENIED` outcome; any other
   `NxsError` an `ok=False` outcome.
4. **Bounds the result** to `max_tool_result_bytes` before it is re-injected as a `tool`
   message. Raw secrets never re-enter the model context.

The `Principal` handed to the Tool Engine is reconstructed by the service from the
session's `initiator_user_id` / `initiator_session_id` (captured at `start_session` from
the API principal). The Tool Engine reads only `user_id` and `organization_id`.

### The loop is bounded and deterministic

`AgentRuntime.run_turn` iterates `range(max_tool_iterations + 1)`:

* a non-tool finish returns `COMPLETED`;
* `iteration >= max_tool_iterations` with the model still asking → `AgentToolLoopLimitError`;
* `len(tool_calls) > max_tool_calls_per_turn` → `AgentOutputInvalidError`;
* duplicate `(name, arguments_hash)` **within a turn** reuses the first outcome — a model
  loop hammering one call cannot cause repeated side effects;
* each tool call carries a derived idempotency key
  (`seed:iteration:call_id:arguments_hash`) into P08's durable idempotency.

A failed turn (loop limit, provider error, invalid output) marks the **turn** FAILED and
returns the **session** to `ACTIVE` — the session survives and stays usable.

## Consequences

* Tool authority is auditable without any model reasoning trace: the allow-list, the
  RBAC grant and the P08 policy decision are the record.
* An agent with an empty `tool_keys` cannot act externally regardless of what the model
  emits.
* Infinite tool loops always terminate with a stable `NXS_AGENT_TOOL_LOOP_LIMIT`.
