# ADR-0093: The bounded tool-execution loop and where tool authority lives

Status: Accepted — NXS-P13 (`NXS-AGENT-001`), 2026-09-10; amended 2026-09-10 by the
NXS-P13 independent-audit corrective #1 (turn-wide semantic tool deduplication; a true
turn-wide tool-call budget; a byte-exact Unicode result bound — see the loop section).

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

### The loop is bounded and deterministic — every guard is turn-wide

`AgentRuntime.run_turn` iterates `range(max_tool_iterations + 1)`, carrying two
turn-scoped structures across **every** iteration:

* **`executed`** — a cache keyed on the semantic identity `(tool_key,
  canonical_arguments_hash)`. The same semantic call reappearing anywhere in the turn —
  in the same response or a later iteration — reuses the first `ToolCallOutcome`; the
  Tool Engine is invoked once, the model still receives a valid result for each protocol
  `tool_call_id`, and `on_tool` (the durable record + `agent.tool.*` events) fires once.
  The model-generated `tool_call_id` correlates the protocol response only — it never
  keys the cache.
* **`tool_calls_requested`** — a counter incremented for **every** model-requested call,
  duplicates included (a re-request is itself loop behaviour). The call that would push
  it past `max_tool_calls_per_turn` raises `AgentToolLoopLimitError` **before** it
  reaches the Tool Engine, so the number of external executions never exceeds the
  ceiling.

Per-iteration guards remain:

* a non-tool finish returns `COMPLETED`;
* `iteration >= max_tool_iterations` with the model still asking → `AgentToolLoopLimitError`;
* `len(response.tool_calls) > max_tool_calls_per_response` (a distinct, structural
  per-response bound — **not** the turn budget) → `AgentOutputInvalidError`.

Each tool call carries a **semantic** P08 idempotency key,
`agt-<sha256(f"{session}:{turn_sequence}:{tool_key}:{arguments_hash}")>` — no
`tool_call_id`, no iteration index. The same semantic call anywhere in one turn resolves
to one P08 durable effect; the identical call in a *later* turn (different sequence) may
legitimately re-run.

Every re-injected tool result is passed through `bound_reinjection`: the final object's
JSON serialisation is `<= max_tool_result_bytes` UTF-8 bytes for ASCII **and** arbitrary
Unicode — measured on the widest serialisation (`ensure_ascii` + `", "` separators, never
shorter than the compact form the runtime writes), trimmed on whole code points, wrapper
overhead (`{"truncated": true, "preview": …}`) included.

A failed turn (loop limit, tool-budget, provider error, invalid output) marks the
**turn** FAILED and returns the **session** to `ACTIVE` — the session survives and stays
usable.

## Consequences

* Tool authority is auditable without any model reasoning trace: the allow-list, the
  RBAC grant and the P08 policy decision are the record.
* An agent with an empty `tool_keys` cannot act externally regardless of what the model
  emits.
* Infinite tool loops always terminate with a stable `NXS_AGENT_TOOL_LOOP_LIMIT`.
