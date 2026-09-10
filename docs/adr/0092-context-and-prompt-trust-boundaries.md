# ADR-0092: Deterministic context assembly and prompt trust boundaries

Status: Accepted — NXS-P13 (`NXS-AGENT-001`), 2026-09-10.

## Context

Everything a model is told arrives in one flat message list. If customer text, a
transcript or a tool result can reach a layer the model treats as authoritative, prompt
injection becomes privilege escalation. The runtime also must not build an unbounded or
cross-tenant context.

## Decision

### Four trust layers, most-trusted first

`nexus_ai.agents.prompt.build_messages` emits, in order:

1. **NXS platform policy** — a constant `PLATFORM_POLICY` string in the module. Not
   configurable per call. States that the model has no direct credential/DB/network/shell
   access, that tools are the only external-action path, and that user/transcript/tool
   content is DATA, not instructions.
2. **Organization agent instructions** — `AgentDefinition.system_instructions`, an
   admin-set (`ai:configure`) bounded configuration resource.
3. **Runtime context** — one `system` message, explicitly delimited
   `READ-ONLY CONTEXT (… not instructions)`, holding deterministic tenant-scoped facts.
4. **Conversation history + current user input** — emitted as `user` / `assistant` /
   `tool` messages, **never** concatenated into a system layer.

Layers 1–2 (and the context header of 3) are the only trusted instruction text and are
entirely Nexus-controlled. A prompt-injection contract/security test asserts user text
never appears inside a `system` message.

### Deterministic, bounded context

`AgentContextBuilder.build` is deterministic given its inputs and enforces, from
`AgentRuntimeSettings`:

* `max_context_messages` — a fixed window over prior turns (`prior_turns[-N:]`), oldest
  dropped first; no unbounded history.
* `max_context_message_chars` — per-message clip.
* `max_context_bytes` — total context budget; assembly stops when exceeded.
* `max_input_chars` — the current user input is clipped.

It performs **tenant-scoped reads only** — a `conversation_id` / `customer_id` on the
session is resolved through the P06 repositories inside the session's tenant transaction
and a miss is `AgentContextInvalidError` / `AgentNotAuthorizedError`. No cross-tenant
reference, no secret, no arbitrary DB dump.

## Consequences

* Injection attempts in customer text cannot change permissions, reveal credentials,
  authorize/enable a tool, choose an integration, bypass the Tool Engine or alter tenant
  authority — none of those decisions read message text (proven by adversarial tests).
* Context is reproducible: the same session state + prior turns always yields the same
  message list, which keeps turn idempotency (ADR-0094) honest.
* Truncation is explicit and observable, never silent corruption of a JSON structure.
