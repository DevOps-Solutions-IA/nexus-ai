# ADR-0094: Cancellation, concurrency and idempotency in the agent runtime

Status: Accepted — NXS-P13 (`NXS-AGENT-001`), 2026-09-10; amended 2026-09-10 by
independent-audit corrective #1 (the tool-call idempotency key is a pure semantic
identity — no `tool_call_id`, no iteration index) and corrective #2 (the per-Agent turn
deadline, the enforced absolute session lifetime with a truthful `EXPIRED` terminal, and
the stable `NXS_AGENT_TURN_TIMEOUT` taxonomy — see the deadline section).

## Context

An agent session is long-lived and driven concurrently: a user submits turns, a voice
barge-in cancels one mid-generation, a worker restarts, a provider delivers a duplicate
completion. The runtime must be terminal-safe, concurrency-safe and idempotent, and must
never surface a stale answer after a cancellation.

## Decision

### Session and turn state machines

`AgentSessionState`: `PENDING → ACTIVE → WAITING_TOOL → RESPONDING →
COMPLETED | FAILED | CANCELLED | EXPIRED`. `AgentTurnState`: `PENDING → RUNNING →
AWAITING_TOOLS → FINALIZING → COMPLETED | FAILED | CANCELLED`. Folds are deterministic
and fail-closed (same design as ADR-0085 / ADR-0089): a terminal state is absorbing, a
terminal proposal always wins from a live state, an undeclared live edge is a no-op.
`state` and `state_rank` are `CHECK`-constrained columns. `EXPIRED` (rank 4, disposition
`EXPIRED`) is the truthful terminal for the absolute lifetime ceiling — neither success
(`COMPLETED`), error (`FAILED`), nor barge-in / client cancellation (`CANCELLED`);
migration `c9e0f1a2b3c4` adds it to the state domain.

### Two independent time bounds

* **Per-turn deadline** — `min(agent.timeout_seconds, settings.agents.turn_deadline_seconds)`
  (`AgentService._effective_turn_deadline`). `agent.timeout_seconds` is the per-Agent
  maximum total-turn execution deadline; a tenant value may only *tighten* the global
  safety ceiling, never widen it. `submit_turn` wraps the whole `_run_turn` task in
  `asyncio.timeout(effective_deadline)` — model calls, the tool loop and continuation
  combined. On expiry the task is cancelled + drained (no stale response committed), the
  turn is persisted `FAILED` with `NXS_AGENT_TURN_TIMEOUT`, `agent.turn.failed` is
  emitted, the **session returns to `ACTIVE`** (a fresh turn is allowed), and
  `AgentTurnTimeoutError` (504) is raised — **never a bare `TimeoutError`**. A single
  model provider call that times out inside `_run_turn` remains a distinct
  `AgentProviderTimeoutError` (`NXS_AGENT_PROVIDER_TIMEOUT`).

* **Absolute session lifetime** — `started_at + settings.agents.max_session_seconds`.
  `_open_turn` checks `_lifetime_exceeded(session, now)` (inclusive: `now >= deadline`)
  under the `SELECT … FOR UPDATE` session-row lock, **before** any model or Tool Engine
  call. Once exceeded the session is terminalised `EXPIRED` + `ended_at` +
  `error_code = NXS_AGENT_SESSION_EXPIRED` in that same locked transaction,
  `agent.session.expired` is emitted, and `AgentSessionExpiredError` (409) is raised —
  no turn row opens, no model call begins, and the terminal is absorbing (no
  resurrection). Concurrent submits serialise on the row lock, so a late turn cannot slip
  through.

### Concurrency

* One `asyncio.Lock` per session id. `submit_turn` refuses with `AgentBusyError` if the
  lock is held or an active turn row exists (`AgentTurnRepository.active_for_session`).
* Every session/turn state write is a `SELECT … FOR UPDATE` inside the tenant
  transaction, so two workers serialize on the row.
* Two simultaneous turns → exactly one runs, the other gets `AgentBusyError` and opens no
  row (proven under a real-PostgreSQL race).

### Cancellation is first-class

`_terminalize` (stop / cancel) **cancels the in-flight turn task before contending for
the per-session lock** — `submit_turn` holds that lock for the whole turn, so acquiring
it first would make cancellation wait for the turn to finish naturally. The cancelled
task unwinds through `submit_turn → _fail_turn(cancelled=True)`, which marks the turn
`CANCELLED`/`FAILED` and returns the session toward the terminal transition. `_run_turn`
does **no** database work, so a cancelled turn leaks no connection. A model answer that
lands during cancellation is discarded — `response_text` stays `NULL` (proven).
`shutdown()` cancels every tracked turn task and clears the locks.

### Idempotency

Canonical SHA-256 fingerprints (`nexus_ai.agents.idempotency`, same rigour as
NXS-P11/P12):

* **start session** — `agent_id · channel · conversation_id · customer_id · call_id ·
  voice_session_id` (v1). Same key + identical fingerprint → replay the winner's session;
  same key + different fingerprint → `AgentIdempotencyConflictError`; concurrent
  duplicates → exactly one session (`IntegrityError` winner resolution).
* **submit turn** — `session_id · sha256(content)` (v1), plus a partial unique index
  `(organization_id, session_id, idempotency_key) WHERE idempotency_key IS NOT NULL`. A
  replay returns the completed turn; a differing body under the same key conflicts;
  exactly one model call happens (proven).
* **tool calls** — a derived semantic key
  `agt-<sha256(session:turn_sequence:tool_key:arguments_hash)>` flows into P08's durable
  idempotency (ADR-0093). It deliberately omits the model `tool_call_id` and the loop
  iteration, so the identical semantic call repeated anywhere in one turn is one external
  effect; the same call in a later turn (new sequence) is a new key and may re-run.

`correlation_id` is observational and excluded from every fingerprint.

## Consequences

* A barge-in stops generation promptly and leaves no "ghost" response.
* A retried HTTP request, a duplicate queue delivery and a double-clicked UI all collapse
  to one effect.
* Because context assembly is deterministic (ADR-0092), a turn replay is a true replay.
