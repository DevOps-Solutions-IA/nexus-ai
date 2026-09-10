# ADR-0094: Cancellation, concurrency and idempotency in the agent runtime

Status: Accepted — NXS-P13 (`NXS-AGENT-001`), 2026-09-10.

## Context

An agent session is long-lived and driven concurrently: a user submits turns, a voice
barge-in cancels one mid-generation, a worker restarts, a provider delivers a duplicate
completion. The runtime must be terminal-safe, concurrency-safe and idempotent, and must
never surface a stale answer after a cancellation.

## Decision

### Session and turn state machines

`AgentSessionState`: `PENDING → ACTIVE → WAITING_TOOL → RESPONDING →
COMPLETED | FAILED | CANCELLED`. `AgentTurnState`: `PENDING → RUNNING → AWAITING_TOOLS →
FINALIZING → COMPLETED | FAILED | CANCELLED`. Folds are deterministic and fail-closed
(same design as ADR-0085 / ADR-0089): a terminal state is absorbing, a terminal proposal
always wins from a live state, an undeclared live edge is a no-op. `state` and
`state_rank` are `CHECK`-constrained columns.

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
* **tool calls** — a derived key `seed:iteration:call_id:arguments_hash` flows into P08's
  durable idempotency (ADR-0093).

`correlation_id` is observational and excluded from every fingerprint.

## Consequences

* A barge-in stops generation promptly and leaves no "ghost" response.
* A retried HTTP request, a duplicate queue delivery and a double-clicked UI all collapse
  to one effect.
* Because context assembly is deterministic (ADR-0092), a turn replay is a true replay.
