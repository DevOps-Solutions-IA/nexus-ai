# ADR-0094: Cancellation, concurrency and idempotency in the agent runtime

Status: Accepted — NXS-P13 (`NXS-AGENT-001`), 2026-09-10; amended 2026-09-10 by
independent-audit corrective #1 (semantic tool-call idempotency key), corrective #2 (the
per-Agent turn deadline, the enforced absolute session lifetime with a truthful `EXPIRED`
terminal, the stable `NXS_AGENT_TURN_TIMEOUT` taxonomy) and corrective #3 (terminal-state
absorption at the DATABASE boundary — `_finish_turn` never resurrects a session another
worker terminalised — and the whole-turn deadline bounded by the session's *remaining*
absolute lifetime so a turn opened just before expiry cannot outlive it), corrective #4
(one idempotency key == one immutable logical turn == one model execution owner, decided
at the DATABASE boundary; a truthful stale-terminal `error_code`), and corrective #5
(response-EXACT replay — `tool_call_count` / `response_correlation_id` persisted as
immutable facts — and historical-replay-lookup ordering: the idempotency key is resolved
BEFORE any new-execution admission gate, so a historical turn survives the session's own
later terminalisation).

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

### The whole-turn deadline

`submit_turn` wraps the whole `_run_turn` task (model calls + tool loop + continuation)
in `asyncio.timeout(effective_deadline)`, where

```
effective_deadline = min(
    settings.agents.turn_deadline_seconds,      # global safety ceiling
    agent.timeout_seconds  (if configured),     # per-Agent total-turn deadline; tightens only
    remaining_session_lifetime,                 # started_at + max_session_seconds - now
)
```

`agent.timeout_seconds` may only *tighten*, never widen, the global ceiling. Including
`remaining_session_lifetime` (`_remaining_session_lifetime`) makes `max_session_seconds`
a true **absolute** ceiling — a turn opened just before expiry is bounded so it cannot
run models / tools past `started_at + max_session_seconds`.

On the deadline the task is cancelled + drained (no stale response committed, no further
model / Tool Engine call). The handler then distinguishes:

* the **session lifetime** elapsed (`_lifetime_exceeded(session, now)`) → the session is
  terminalised `EXPIRED` (`_expire_session_now`, own `FOR UPDATE` transaction), the turn
  is `CANCELLED` with `NXS_AGENT_SESSION_EXPIRED`, and `AgentSessionExpiredError` (409) is
  raised — the canonical session-expired semantics, **not** a misleading turn / provider
  timeout;
* otherwise → the turn is `FAILED` with `NXS_AGENT_TURN_TIMEOUT`, `agent.turn.failed` is
  emitted, the **session returns to `ACTIVE`** (a fresh turn is allowed), and
  `AgentTurnTimeoutError` (504) is raised — **never a bare `TimeoutError`**.

A single model provider call that times out inside `_run_turn` remains a distinct
`AgentProviderTimeoutError` (`NXS_AGENT_PROVIDER_TIMEOUT`).

### Absolute session lifetime — enforced at admission AND in flight

`started_at + settings.agents.max_session_seconds` is the absolute ceiling.

* **At admission** — `_open_turn` checks `_lifetime_exceeded(session, now)` (inclusive:
  `now >= deadline`) under the `SELECT … FOR UPDATE` session-row lock, **before** any turn
  row is inserted. Once exceeded, the session is terminalised `EXPIRED` + `ended_at` +
  `error_code` in that same locked transaction, `agent.session.expired` is emitted, and
  `AgentSessionExpiredError` (409) is raised — no turn opens, no model call begins.

* **In flight** — the deadline above bounds the running turn (see the whole-turn
  deadline); when it fires because the lifetime elapsed, the session is terminalised
  `EXPIRED` and no continuation runs.

### Terminal-state absorption is a DATABASE property, not a process-lock property

The per-session `asyncio.Lock` is process-local — it does not serialise multiple Nexus
backend workers / replicas. Every state transition that writes session state **after a
long external operation** re-reads the row `SELECT … FOR UPDATE`, re-validates the
persisted state, applies the canonical fold, and lets the terminal win:

* `_finish_turn` — after the model task returns, it re-reads the session `FOR UPDATE`. If
  `session_is_terminal(refreshed.state)` (a concurrent worker terminalised it — `EXPIRED`
  / `CANCELLED` / `FAILED` / `COMPLETED`), the stale outcome is **discarded**: the turn is
  marked `CANCELLED` with the session's terminal `error_code` (`_discard_stale_turn`), no
  `response_text` is written, **no `agent.response.ready` is published**, the session is
  **not** resurrected, and `AgentSessionExpiredError` / `AgentInvalidStateError` is
  raised. Otherwise the session move to `ACTIVE` goes through `fold_agent_session_state`.
* `_fail_turn` — already guarded (`not session_is_terminal(session.state)` before any
  `ACTIVE` write).
* `_terminalize` / `_terminalize_expired` — fold-guarded; a no-op on an already-terminal
  row.

`_discard_stale_turn` labels the discarded turn with the **truthful** terminal
`error_code` (`_stale_terminal_code`): the session's own `error_code` if it carries one,
else `NXS_AGENT_SESSION_EXPIRED` (EXPIRED), `NXS_AGENT_CANCELLED` (CANCELLED), or
`NXS_AGENT_INVALID_STATE` (a normal stop / COMPLETED, or FAILED) — a normal stop is
**never** relabelled a session expiration. `_finish_turn` raises the matching error
(`AgentSessionExpiredError` / `AgentCancelledError` / `AgentInvalidStateError`).

Proven with **two independent `AgentService` instances sharing one PostgreSQL database**:
worker A opens a turn and blocks in the model; worker B expires the shared row; worker A
resumes into `_finish_turn` and the row stays `EXPIRED` — never `EXPIRED → ACTIVE`, no
stale `response_text`, no `agent.response.ready`.

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
* **submit turn** — `session_id · sha256(content)` (v1), plus a partial UNIQUE index
  `(organization_id, session_id, idempotency_key) WHERE idempotency_key IS NOT NULL`.

  **One idempotency key names ONE immutable logical turn == ONE model execution owner**,
  decided at the DATABASE boundary (not the process-local `asyncio.Lock`). `_open_turn`
  runs inside a `SELECT … FOR UPDATE` session-row transaction and either inserts the turn
  row (this worker is the **owner** — it alone runs the model / tool loop) or resolves an
  existing key via `_resolve_existing_turn`:

  | Existing turn state | Outcome |
  |---|---|
  | fingerprint mismatch | `AgentIdempotencyConflictError` (`NXS_AGENT_IDEMPOTENCY_CONFLICT`) |
  | `COMPLETED` | deterministic replay of the persisted `AgentResponse` — no model / tool call |
  | `RUNNING` / `PENDING` / `AWAITING_TOOLS` / `FINALIZING` | `AgentBusyError` — another worker owns execution; this worker never enters `_run_turn` |
  | `FAILED` / `CANCELLED` | `AgentIdempotentReplayError` (`NXS_AGENT_IDEMPOTENT_REPLAY`, carries `extensions.original_error_code` / `turn_state`) — **never a fresh execution** |

  An `IntegrityError` on the turn insert (the partial UNIQUE index — defence in depth
  behind the row lock) is resolved to `AgentBusyError` (the claim is another worker's). A
  loser worker never enters `_run_turn` — proven with **two independent `AgentService`
  instances sharing one PostgreSQL database**: same org / session / key / content, worker
  A blocks in the model, worker B gets `AgentBusyError`, only one model execution, one
  turn row, one `agent.turn.started`, and the token / tool / session counters are not
  doubled; a later replay returns worker A's persisted result.
* **tool calls** — a derived semantic key
  `agt-<sha256(session:turn_sequence:tool_key:arguments_hash)>` flows into P08's durable
  idempotency (ADR-0093). It deliberately omits the model `tool_call_id` and the loop
  iteration, so the identical semantic call repeated anywhere in one turn is one external
  effect; the same call in a later turn (new sequence) is a new key and may re-run.

`correlation_id` is observational and excluded from every fingerprint.

### Historical-replay ordering

`_open_turn` resolves the idempotency key **before** any new-execution admission gate
(terminal state, absolute lifetime, one-turn-at-a-time). A historical turn's outcome
(`COMPLETED` replay, `FAILED`/`CANCELLED` immutable-terminal replay, or an active-turn
`AgentBusyError`) is valid regardless of what has since happened to the **session** — a
session that later became `COMPLETED`/`CANCELLED`/`EXPIRED` must never make a historical
key unreachable, because the key names the **turn**, not the session's current state.
Only when no historical turn matches the key (a genuinely new key, or none supplied) do
the session's admission gates apply — to that new attempt, never to a replay.

### Response-exact replay

The first successful response and every later replay of the same key are **field-exact**
(`replay.model_dump() == original.model_dump()`). Two facts a replay cannot otherwise
reconstruct are persisted once, immutably, on `ai_agent_turns` at `_finish_turn`
completion (migration `d0e1f2a3b4c5`):

* **`tool_call_count`** — the number of DISTINCT tool calls actually executed this turn
  (`len(outcome.tool_calls)`). This is **not** `tool_iterations` (the loop-iteration
  count) — the two differ whenever one model response requests more than one tool call at
  once, and reconstructing `AgentResponse.tool_calls` from `tool_iterations` was corrective
  #5's headline defect.
* **`response_correlation_id`** — the effective correlation id the original response
  published under (`ctx_correlation(request, session)`), so a replay never substitutes a
  fabricated `null`.

**Chosen design — persist the facts on the turn row (not derive-on-read):** `tool_call_count`
could instead be derived at replay time from `count(*) FROM ai_agent_tool_calls WHERE
turn_id = …` (the row count is provably identical — the runtime's `on_tool` callback fires
exactly once per distinct executed call). Persisting was chosen over deriving because (a)
it keeps a completed turn a **self-contained immutable record** — a future retention
policy on `ai_agent_tool_calls` cannot silently change what a turn replays as; (b) it costs
no extra query/transaction on the hot replay path (`_response_from_turn` stays a pure,
synchronous reconstruction); (c) it is the natural extension of the same principle already
applied to `input_tokens`/`output_tokens`/`latency_ms`, which are likewise persisted facts,
not live derivations. Both new columns are non-secret operational facts (a count, a
caller-supplied correlation id) — no chain-of-thought, no provider body, no credential.
`response_correlation_id` cannot be backfilled for a turn that completed before this
migration (the fact was never persisted anywhere); such a turn's replay reports
`correlation_id: null`, exactly as it did before the fix — a non-regression, not a new
defect. `tool_call_count` **is** backfilled from the durable `ai_agent_tool_calls` rows in
the same migration, so no pre-existing turn regresses to a wrong `0`.

## Consequences

* A barge-in stops generation promptly and leaves no "ghost" response.
* A retried HTTP request, a duplicate queue delivery and a double-clicked UI all collapse
  to one effect.
* Because context assembly is deterministic (ADR-0092), a turn replay is a true replay.
