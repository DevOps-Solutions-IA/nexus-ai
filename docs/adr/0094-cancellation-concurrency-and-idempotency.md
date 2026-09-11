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
later terminalisation), and corrective #6 (distributed execution control: an
authoritative cancellation/terminalisation committed by ANY worker stops model
continuation and tool dispatch for a DIFFERENT worker's in-flight turn at the next
checkpoint — not merely at final-commit time — plus the durable `execution_owner_id` /
`lease_expires_at` primitives an orphaned-RUNNING-turn recovery mechanism will need,
deferred to NXS-P25 per requirement `NXS-AGENT-002`), and corrective #7 (linearizable
execution authority: the freshest-possible unlocked read corrective #6 used immediately
before a NEW tool dispatch narrowed the TOCTOU window between authorization and dispatch
but did not close it; a durable `ai_agent_tool_dispatch_permits` linearization point —
serialized on the SAME row lock cancellation uses — now makes the ordering between a new
tool's authorization and a concurrent cancellation a PROVABLE, PostgreSQL-enforced fact
rather than a probabilistic one).

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

### Distributed cancellation — execution control across workers (corrective #6)

Corrective #3 made a terminal SESSION absorbing at `_finish_turn`'s DATABASE boundary — a
stale worker's FINAL response is always discarded. That is **not sufficient**: between
the moment a DIFFERENT worker commits a cancellation and the moment the stale worker
reaches `_finish_turn`, the stale worker could still call the model again AND dispatch
NEW tool calls to the NXS-P08 Tool Engine — real external side effects a discarded
response cannot undo.

**Chosen architecture — PostgreSQL authoritative, checkpoint-based, no pub/sub
dependency (a variant of Option B, "durable turn claim", not Option D/E's NATS
propagation):** `AgentRuntime.run_turn` accepts a `check_authority` callback
(`AgentService._check_execution_authority`) and calls it at every natural loop boundary:

* at the **top of every continuation iteration**, before the next model call;
* **immediately** before every NEW tool dispatch (never before a deduplicated,
  already-executed call — that never reaches the Tool Engine at all).

The checkpoint is a single UNLOCKED read of the session's current committed `state`.
PostgreSQL's read-committed isolation means any worker's already-COMMITTED terminal
transition (`cancel_session` / `stop_session` / lifetime expiry — all still `SELECT …
FOR UPDATE`, unchanged from corrective #3) is visible to this read without a row lock; a
row lock buys mutual exclusion, which a freshness check does not need. The moment the
session is terminal, the checkpoint raises an internal `ExecutionRevoked(session_row)` —
never an `NxsError`, never surfaced directly — which unwinds `run_turn` and is caught in
`submit_turn`, which fails the turn through the SAME `_fail_turn` path corrective #4/#5
already use (its own `not session_is_terminal(...)` guard means it can never resurrect
the session) and raises the TRUTHFUL taxonomy error via the shared `_raise_for_stale_session`
helper — the identical function `_finish_turn`'s pre-existing terminal-absorption tail
now also calls, so there is exactly one place that decides "EXPIRED → session-expired,
CANCELLED → cancelled, else → invalid-state", used by both the final-commit path and the
new mid-turn path.

**Why NOT a NATS fast-path (Option D/E) in this corrective:** every checkpoint is already
bounded by `max_tool_iterations` (≤ 32) and `max_tool_calls_per_turn` (≤ 32) — a
single-digit-to-low-double-digit number of extra lightweight SELECTs per turn, not a
per-streamed-token cost. This closes the required races within the stated tolerance
without adding a pub/sub delivery dependency to a correctness path; a NATS fast-path
would only shrink the already-small window between a commit and the next checkpoint
observing it — a latency optimisation, not a correctness requirement — and is left for
future work if operational experience ever shows the checkpoint cadence insufficient.
PostgreSQL alone remains authoritative either way, matching the mandate that correctness
never depend exclusively on pub/sub delivery.

**Fencing / TOCTOU — precisely what is, and is not, guaranteed.** Between the checkpoint
observing "not yet terminal" and the actual `await self._tools.execute(...)` call there
is an irreducible gap — no process can atomically read a remote database and dispatch a
remote call in the same instant without holding a lock across the external I/O, which
this design deliberately does not do (holding a DB row lock across a Tool Engine HTTP
call would serialize unrelated turns against it). The documented, tested boundary is:

> Operations DISPATCHED before the authoritative cancellation commits may complete —
> they are not retracted. No operation is DISPATCHED after the authoritative
> cancellation has committed and been observed by this turn's next checkpoint.

`tests/concurrency/test_agent_cross_worker_cancellation.py::test_already_dispatched_tool_completes_but_nothing_further_dispatches`
proves the first half (a real in-flight HTTP call to the mock Tool Engine backend, mid-
flight when cancellation commits, is allowed to finish) and every cross-worker test in
that file proves the second half (the NEXT dispatch — the checkpoint immediately
preceding it — is blocked once cancellation has committed). This is the same class of
unavoidable race every check-then-act distributed system has; no claim of zero-gap
fencing is made, and none is needed to satisfy INV-CANCEL-001/005/006/009.

**Why P08 itself is not extended:** the checkpoint sits entirely in P13, immediately
before `AgentToolBridge.execute` is ever called — the Tool Engine never receives a call
for a turn whose session was already observed terminal. Extending P08 with its own
fencing context would duplicate this check for no additional guarantee the immediately-
preceding P13 checkpoint doesn't already provide, and was explicitly out of scope.

**Voice barge-in (P12 → P13):** a barge-in reaches the SAME generic `cancel_session` API
any other worker would call — P13 has no voice-specific cancellation path, so the
guarantee above applies identically to a `VOICE` channel turn with no ElevenLabs-specific
structure added to P13 (proven by
`test_voice_channel_barge_in_cross_worker_cancel`).

**Invariants locked by this section:** INV-CANCEL-001 through INV-CANCEL-009 (the state-
matrix / crash-window discipline established in corrective #3/#5 continues to hold —
this section only adds the mid-turn checkpoint; it changes no terminal-absorption or
replay semantics already proven).

### Orphaned RUNNING turn recovery — durable primitives now, autonomous reaping deferred to NXS-P25 (corrective #6)

**The problem:** `_open_turn` commits a turn `RUNNING` and returns; if the CLAIMING
worker's PROCESS then dies (not merely cancels a task — an actual crash, OOM-kill, or
hard network partition), nothing ever calls `_fail_turn` / `_finish_turn` for that row.
`AgentTurnRepository.active_for_session` treats any non-terminal turn as active, so:
same idempotency key → `AgentBusyError` forever; a brand-new key → also `AgentBusyError`
forever (`active_for_session` blocks admission regardless of key). The session is
BUSY indefinitely — this is a real, currently-unsolved limitation, not fixed by P13.

**Decision — Option B:** establish the minimum durable primitives an eventual recovery
mechanism needs, defer the recovery mechanism itself to **NXS-P25 (Resilience)**, and do
NOT claim crash recovery from P13. Requirement `NXS-AGENT-002` (target phase NXS-P25,
`status: PLANNED`, `dependencies: [NXS-AGENT-001]`) records this machine-readably in
`.nxs/requirements.json` — pure forward-looking governance metadata, no execution state
mutated, following the identical precedent set by `NXS-AGENT-001` itself when P13's own
governance blocker was corrected.

**The primitives (migration `e1f2a3b4c5d6`, `ai_agent_turns`):**

* **`execution_owner_id`** — a random token minted once, at claim time. P13 never reads
  it to make a decision (a turn is already claimed exclusively at the DATABASE boundary
  by the partial UNIQUE index / `active_for_session` check — this column does not change
  that). It exists purely so a future reaper can label which attempt it recovered from.
* **`lease_expires_at`** — set once, at claim time, to `now() + min(the global turn
  deadline ceiling, the session's remaining absolute lifetime) + execution_lease_grace_seconds`
  (default grace 30s). This is a conservative UPPER BOUND on how long a LEGITIMATE worker
  could still be executing: `submit_turn` wraps the ENTIRE model+tool loop in
  `asyncio.timeout` using that same bound (a tenant's `agent.timeout_seconds` can only
  TIGHTEN it further, so the lease may slightly OVERESTIMATE — which only delays safe-reap
  eligibility, never falsely shortens it). **A `RUNNING` turn whose `lease_expires_at` has
  passed is therefore an UNAMBIGUOUS signal**: no legitimately-alive worker can still be
  executing it, because its own `asyncio.timeout` would already have fired and driven it
  to a terminal state if it were alive. `test_orphaned_running_turn_is_unambiguous_but_p13_does_not_reap_it`
  proves both halves: the signal is unambiguous AND P13 provably does not act on it (the
  session stays `AgentBusyError` after the lease has expired).

**No permanent ambiguity — the backfill asymmetry is deliberate and documented:** both
columns are nullable and NOT backfilled (migration `e1f2a3b4c5d6`); a turn created before
this corrective has `execution_owner_id IS NULL` / `lease_expires_at IS NULL` forever. A
future NXS-P25 reaper MUST treat `lease_expires_at IS NULL` as "unknown deadline — do not
assume safe to reap", never as "safe to reap" and never as "never expires" — this is the
same non-ambiguous-null discipline already used for `response_correlation_id`.

**The margin is a practical engineering buffer, not a formally proven bound (corrective
#6 final-audit LOW finding):** `lease_bound` is proven `<=` the real enforced
`asyncio.timeout` deadline by construction (both are `min(global ceiling, remaining
session lifetime)`, and the effective deadline can only be tighter via a per-Agent
override). The `execution_lease_grace_seconds` margin added on top (default 30s) is sized
to absorb the residual scheduling gap between claiming the row and `asyncio.timeout`
actually starting, plus the time `_fail_turn` / `_expire_session_now` need to commit
after a legitimate timeout fires — but that absorption is an operational judgement, not a
mathematical proof. Because P13 performs no reaping on this signal today, the distinction
has no current effect. A future NXS-P25 reaper MUST NOT treat `lease_expires_at` as an
instant-safe cutoff — it should apply its OWN additional operational safety margin on top
before treating an expired lease as reapable, rather than trusting this bound as exact.

**What P13 certification does NOT claim:** it does not claim autonomous crash recovery,
does not reap orphaned turns, and does not change `active_for_session`'s current
behaviour. `INV-CRASH-001` ("worker crash produces a recoverable, not ambiguous, durable
state") is satisfied at the PRIMITIVE level — the state IS unambiguous and IS
recoverable — not at the AUTOMATION level, which NXS-P25 owns.

### Tool-dispatch linearization / fencing (corrective #7)

**Why corrective #6 was not enough.** `_check_execution_authority` — a cheap, UNLOCKED
read, called immediately before every new tool dispatch — narrows the TOCTOU window
between "is this session still authoritative" and "dispatch to the Tool Engine" to the
smallest interval that code structure allows. It does not CLOSE it: PostgreSQL's
read-committed isolation lets that unlocked SELECT return the last COMMITTED snapshot
even while a concurrent cancellation transaction is ALREADY IN FLIGHT (its own `SELECT …
FOR UPDATE` already acquired, its `UPDATE` not yet issued) — an unlocked reader never
waits on another transaction's uncommitted row lock. So a cancellation that is, in every
meaningful sense, already "happening" can still lose an unlocked race against a read that
started (and finished) a moment earlier. This is proven, not asserted:
`tests/concurrency/test_agent_tool_dispatch_fencing.py::test_authorize_blocks_on_inflight_cancellation_and_correctly_rejects`
holds a real cancellation transaction open (its `UPDATE` deliberately delayed past its
already-acquired row lock) and shows the tool genuinely dispatches
(`seen == ["/c/x1"]`) against the corrective-#6-only mechanism — a real, reproducible
violation, not a hypothetical one.

**The fix — a durable linearization point, not another read.** `AgentService.
_authorize_tool_dispatch` replaces the pre-dispatch checkpoint for NEW tool calls
specifically (the model-continuation checkpoint is untouched — see above). It:

1. Opens a transaction and takes `SELECT … FOR UPDATE` on the OWNING SESSION ROW — the
   IDENTICAL lock `_terminalize` (stop / cancel / expire) takes.
2. If the session is already terminal under that lock, raises `ExecutionRevoked` — no
   permit is ever created, no dispatch follows.
3. Otherwise, INSERTS a durable `ai_agent_tool_dispatch_permits` row (migration
   `f2a3b4c5d6e7`) — `(organization_id, session_id, turn_id, sequence, tool_key,
   arguments_hash, authorized_at)` — and commits.

Two transactions contending for the SAME row lock are serialized by PostgreSQL itself,
not by application logic: whichever transaction's lock request is granted FIRST runs to
completion (commit or rollback) before the other's is even granted. This makes the
"did this tool call's authorization happen before or after the cancellation" question a
DURABLE, OBJECTIVELY PROVABLE fact — the commit order — rather than a race decided by
which coroutine happened to run first:

* the permit transaction wins the lock → observes the session ACTIVE → commits the
  permit → the caller may dispatch, and that dispatch remains valid even if cancellation
  commits a moment later (**Case 1**, INV-FENCE-003);
* the cancellation transaction wins the lock and commits first → the permit transaction,
  once granted the lock, observes the session already terminal → rejected, no permit, no
  dispatch (**Case 2**, INV-FENCE-002).

`AgentToolBridge.execute` (the actual external HTTP call) always runs ENTIRELY OUTSIDE
this transaction — only the DB round trip for the permit is held under the lock, matching
the explicit preference against holding an ordinary transaction open across a remote call.

**Lock-starvation tradeoff (final-audit LOW finding, made explicit):** because every
transaction that touches the session row — `_open_turn`, `_finish_turn`, `_fail_turn`,
`_terminalize`, and now `_authorize_tool_dispatch` — is short and DB-only (never a remote
call held under the lock), a single stuck/slow worker cannot block other operations on the
same session for longer than one local DB round trip. This is the SAME tradeoff correctives
#3/#4/#6 already accept implicitly (the session row has always been a serialization point
for terminalisation and turn-claiming); this corrective adds one more short, bounded
contender for that same lock, not a new class of risk.

**Why not an epoch/fencing-generation column (Option B), a P08-owned authorization
reservation (Option C), or an advisory lock (Option D).** The session row itself, already
the sole terminalisation authority since corrective #3, is the natural and sufficient
serialization boundary — introducing a SEPARATE epoch counter or advisory-lock namespace
would duplicate that authority without closing any additional gap: whatever advances or
checks an epoch would itself need to serialize against the SAME session-terminalisation
transaction to be correct, at which point the row lock already used is doing the real
work. A P08-owned reservation (Option C) would require extending P08's own execution
model for a guarantee P13's own row lock already provides without it — out of scope and
unnecessary; **P08 is entirely unchanged by this corrective**, and never receives, holds
or validates any fencing token — it continues to see exactly the same governed
`ToolInvocation` calls it always has, and every existing P08 idempotency guarantee (ADR
0093) is undisturbed.

**Ownership / authorization semantics — precise terminology (per the directive's own
discipline).** Four DISTINCT concepts now exist and must not be conflated:

* **execution claim** — the exclusive right to run a turn's model/tool loop at all,
  decided by the partial UNIQUE index + `SELECT … FOR UPDATE` at turn-insert time
  (corrective #4). Unaffected by this corrective.
* **lease** — `execution_owner_id` / `lease_expires_at` (corrective #6): a durable,
  NEVER-RENEWED, NEVER-VALIDATED-BY-P13 upper bound for future orphan detection. Still
  not a fencing mechanism; still not read by P13 to make any decision.
* **fencing generation** — NOT introduced by this corrective (see above); the session
  row lock is used directly instead of a separate generation counter.
* **dispatch authorization / permit** — THE NEW CONCEPT this corrective introduces: a
  durable, per-tool-call, linearization-backed record that a specific NEW external
  dispatch was legitimately authorized at a specific, provable point relative to any
  concurrent cancellation. This is the only one of the four that changes this corrective.

**Already-authorized-operation / post-cancel guarantees.** Identical to corrective #6's
documented boundary, now on a provably exact footing rather than a narrowed-probability
one: an operation already DISPATCHED (its permit committed, `AgentToolBridge.execute`
already called) before cancellation commits may complete
(`test_authorize_before_cancel_commit_may_complete`); no operation is DISPATCHED once
cancellation has committed and the next authorization attempt observes it
(`test_authorize_blocks_on_inflight_cancellation_and_correctly_rejects`). A model
response requesting several tools in the SAME iteration is authorized one at a time, in
order — an earlier one already authorized/dispatched before cancellation commits may
complete; a later one in the SAME response, authorized only after cancellation has
committed, is rejected exactly like any other new dispatch
(`test_multi_tool_same_response_only_pre_cancel_authorization_survives`).

**Crash-window analysis (extends corrective #3's / #6's).**

| Window | Durable state | Recovery owner | Duplicate-effect risk |
|---|---|---|---|
| P1: permit commits, worker dies before the external call | An `AUTHORIZED` permit row with no matching `ai_agent_tool_calls` row for the same `(turn_id, tool_key, arguments_hash)` | NXS-P25 (deferred, not implemented here) | None BY ITSELF — no external call was ever made; a future retry under a NEW attempt would need P08's own idempotency key (ADR-0093), unaffected by this corrective |
| P2: the external call succeeds, worker dies before `ai_agent_tool_calls` is recorded | Permit row exists; the external system may have applied the effect; no local record of the outcome | NXS-P25 | Possible only if a FUTURE recovery mechanism blindly retries without consulting P08's own idempotency key — out of scope for P13, which performs no such retry |
| P3: cancellation commits after the permit but before the external call | Permit row exists (legitimately, Case 1); session CANCELLED | Worker A itself — the external call may still be dispatched (already authorized) | None — this is the documented, tested, INTENDED boundary |
| P4: cancellation commits before the permit transaction even starts | No permit row; session CANCELLED | N/A — rejected outright | None — Case 2 |
| P5: a duplicate worker reuses the same tool idempotency key | Structurally unreachable in the current architecture — a turn is exclusively owned by one worker (correctives #1/#4); the permit table's unique index on `(organization_id, turn_id, tool_key, arguments_hash)` is defense-in-depth for this case regardless (INV-FENCE-011) | N/A | None — P08's own idempotency key (ADR-0093) is the final backstop even if this were ever reachable |
| P6: a DB reconnect/retry happens around permit creation | The permit INSERT is a single, atomic, short transaction — a reconnect either lands before it (no permit, safe to retry the whole authorization) or after a successful commit (permit durably exists) | Caller (existing `submit_turn` retry semantics, unaffected) | None |

**No claim of "exactly-once" physical external effect** is made anywhere in this
section — P1/P2 explicitly leave open that a FUTURE recovery mechanism could duplicate an
effect if it ever retried carelessly; this corrective's guarantee is that NO NEW
dispatch is EVER authorized once cancellation has linearized first — it says nothing
stronger about what a not-yet-built P25 recovery mechanism might one day do.

**Invariants locked by this section:** INV-FENCE-001 through INV-FENCE-012.

### NXS-AGENT-002 governance: mandatory=true (corrective #7 §13)

Corrective #6 recorded `NXS-AGENT-002` (orphaned-turn crash recovery, target NXS-P25) as
`mandatory: false` — appropriate at the time, since the underlying limitation (a crashed
worker's RUNNING turn blocks its session `AgentBusyError` forever, with no recovery path)
was newly DISCOVERED and DOCUMENTED, not yet weighed as a governance gate. Corrective #7
re-examines this: an orphaned RUNNING turn causing PERMANENT session unavailability is a
genuine, currently-unmitigated production resilience gap — not a nice-to-have. Per the
corrective #7 directive, `NXS-AGENT-002.mandatory` is changed to `true`, while `status`
stays `PLANNED` and `target_phase` stays `NXS-P25` (P13 still does not implement P25's
work). This was validated against the repository's own lifecycle semantics
(`scripts/nxs_control/core.py`) before being applied: the mandatory-requirement-to-phase-
manifest consistency check (`mapped != expected` in `validate_repository_state`) only
runs against phases that already HAVE a `.nxs/phases/NXS-Pxx.json` manifest file — NXS-P25
has none yet (it has not started), so this change has NO effect on `nxs_validate` today,
and no effect whatsoever on NXS-P13's own closure (P13's manifest is validated only
against requirements whose `target_phase == "NXS-P13"`, i.e. `NXS-AGENT-001`, unaffected).
Its effect is entirely FORWARD: when NXS-P25 is eventually started and its manifest is
created, that manifest's `requirements_implemented` list will be REQUIRED to include
`NXS-AGENT-002` — NXS-P25 cannot reach READY/GO without validating it. Correspondingly,
**NXS-P30 ("Backend Certification") cannot certify backend production readiness while
`NXS-AGENT-002` remains un-`VALIDATED`**, since NXS-P30 depends (transitively, through the
phase dependency chain) on NXS-P25's own closure gate now including this requirement.

## Consequences

* A barge-in stops generation promptly and leaves no "ghost" response.
* A CROSS-WORKER barge-in / cancel / stop / expiry stops a DIFFERENT worker's model
  continuation and tool dispatch at the next checkpoint, not merely at final commit — an
  external side effect already dispatched before that commit may still complete (a
  precisely documented and tested boundary), but nothing NEW starts after it.
* An orphaned RUNNING turn (its worker crashed) is a durable, unambiguous fact a future
  NXS-P25 mechanism can safely recover from — P13 does not claim to recover it itself —
  and NXS-P25's own closure now REQUIRES it be solved (`NXS-AGENT-002` is mandatory).
* A NEW tool dispatch's authorization relative to a concurrent cancellation is a provable,
  PostgreSQL-linearized fact, not a narrowed-probability read — closing the residual
  TOCTOU gap corrective #6 could reduce but not eliminate.
* A retried HTTP request, a duplicate queue delivery and a double-clicked UI all collapse
  to one effect.
* Because context assembly is deterministic (ADR-0092), a turn replay is a true replay.
