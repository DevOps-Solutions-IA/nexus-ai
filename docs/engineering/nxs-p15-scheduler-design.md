# NXS-P15 Scheduler — Pre-Implementation Contract

Status: governance design only. This document does not start NXS-P15, authorize implementation, create schema, authorize merge, or authorize deployment. The canonical requirement is `NXS-SCHED-001`; the implementation branch, after this governance change is merged and exact `main` is green, is `feat/nxs-p15-scheduler`.

## Purpose and authority chain

P15 answers **when a governed workflow should be activated**. P14 remains authoritative for **how that workflow executes**.

```text
P15 schedule
  -> P15 occurrence
  -> P14 Workflow Engine start with stable idempotency identity
  -> TOOL through P08 or AGENT through P13
```

The only P15 target in the initial contract is `START_WORKFLOW`, referencing an immutable, published, same-tenant P14 `workflow_version_id`. P15 never executes customer or business effects, invokes a tool or agent directly, accepts an arbitrary executable payload, or routes to a provider.

## Ownership

P15 owns durable schedule definitions; one-time and recurring schedules; constrained recurrence; IANA timezone interpretation; UTC occurrence timestamps; deterministic next-occurrence calculation; just-in-time occurrence materialization; PostgreSQL-authoritative due claims; multi-worker fencing; occurrence idempotency; governed P14 dispatch; schedule activation, pause, resume, cancellation and completion; cancellation of undispatched occurrences; bounded misfire handling; optimistic schedule revisions; bounded dispatch retries; transition history; P04 scheduler events; forced-RLS tenant isolation; bounded APIs; RBAC integration; clock-skew policy; DST semantics; recurrence horizon validation; and bounded scheduler batches.

P15 does not own campaigns, audiences, bulk outreach or campaign throttling (P16); human queue scheduling, shifts, assignment or routing (P17); cell placement/scaling (P18); SIP routing/scaling (P19); autonomous operations (P20); billing (P23); the complete observability platform (P24); automatic orphan recovery, stale-owner reassignment, ambiguous-dispatch reconciliation or broad failover (P25); backup/DR (P26); or deployment (P32).

P15 rejects shell, SQL, arbitrary HTTP, arbitrary Python/code execution, operating-system cron management, Kubernetes/Nomad infrastructure scheduling, direct P08/P13/provider/channel/telephony calls, credentials and raw authorization material.

## Architectural invariants

- **INV-SCHED-001 — Tenant isolation.** Every schedule, occurrence and transition belongs to exactly one `organization_id`. Forced PostgreSQL RLS and tenant-aware composite foreign keys enforce same-tenant P14 workflow references and scheduler relationships.
- **INV-SCHED-002 — PostgreSQL authority.** Schedules, revisions, temporal cursors, occurrences, claims, owner/token, dispatch state, cancellation, pause and idempotency live durably in PostgreSQL. Process-local timers may wake a worker but never grant authority.
- **INV-SCHED-003 — P14-only dispatch.** `START_WORKFLOW` is the sole initial target, and the only dispatch boundary is the P14 service/API contract. P15 cannot call P08, P13, messaging, telephony or an external provider directly.
- **INV-SCHED-004 — Immutable occurrence semantics.** An occurrence snapshots the schedule revision, target workflow version, intended local slot, UTC instant, timezone, UTC offset/fold metadata, misfire policy and canonical occurrence key. Later schedule edits cannot mutate it.
- **INV-SCHED-005 — Single occurrence authority.** At most one current owner/token can authorize dispatch for an occurrence. Claims and results use PostgreSQL row locking and compare-and-set predicates.
- **INV-SCHED-006 — Logical idempotency.** One canonical occurrence maps to at most one logical P14 run identity. Physical exactly-once execution is not claimed.
- **INV-SCHED-007 — Bounded time.** Recurrence search, lookahead, batch size, dispatch retries and catch-up are bounded. No recurrence can generate an unbounded immediate backlog.
- **INV-SCHED-008 — Database time ordering.** Due selection and control races use PostgreSQL time and transaction order. Worker clocks only determine sleep duration and cannot authorize early dispatch.
- **INV-SCHED-009 — Terminal absorption.** Cancelled and completed schedules do not reactivate. Dispatched, failed, skipped and cancelled occurrences do not return to claimable state.
- **INV-SCHED-010 — P25 recovery boundary.** P15 persists enough ownership and dispatch identity to identify stale or ambiguous claims but never automatically reaps, reassigns or reconciles them in this phase.

## Schedule and recurrence contracts

`ScheduleType` is a closed enum:

- `ONE_TIME`: exactly one future UTC occurrence derived from one timezone-qualified local intent or an explicit UTC instant.
- `RECURRING`: a constrained structured calendar recurrence interpreted in an IANA timezone.

P15 deliberately chooses a structured recurrence contract rather than accepting arbitrary host-cron text. The proposed recurring shape contains a closed `frequency` (`MINUTELY`, `HOURLY`, `DAILY`, `WEEKLY`, `MONTHLY`), a bounded positive `interval`, optional weekdays or month days where applicable, a local wall-clock time where applicable, an IANA `timezone`, an inclusive start boundary and optional exclusive end boundary. Invalid field combinations are rejected with `extra="forbid"`.

Minimum cadence is one minute. Interval, weekday/month-day sets, lookahead search and calculation steps are bounded. The calculator must either produce the next occurrence inside the configured horizon or return a stable validation/exhaustion error; it may not loop indefinitely. An indefinite recurring schedule is permitted, but each calculation searches at most five years or 525,600 candidate minute slots, whichever bound is reached first.

## Timezone, DST and clock semantics

Authoritative occurrence instants are stored as timezone-aware UTC timestamps. User/business intent retains the IANA timezone identifier and canonical local slot. Naive local timestamps are never the dispatch authority.

The production artifact pins the timezone database dependency. Every materialized occurrence records the timezone identifier, intended local wall time, UTC offset, fold value and timezone-data version used for resolution. Already materialized occurrences never move after a timezone-rule update. Future just-in-time occurrences use the active pinned rules and record their version; serialized materialization plus the canonical local-slot key prevents duplicate creation during a rolling rule update.

- **DST spring-forward gap:** a nonexistent local slot is `SKIPPED` with reason `DST_NONEXISTENT_LOCAL_TIME`; it is not silently shifted to another wall time.
- **DST fall-back fold:** only the earlier valid instant (`fold=0`) is materialized by default. The local-slot occurrence key prevents a second logical occurrence for `fold=1`.
- **Timezone rule change:** materialized UTC instants are immutable; future materialization records the new pinned rules. A material difference is visible in transition/event metadata, never retroactively rewritten.
- **Process restart:** PostgreSQL `next_fire_at` and occurrence rows reconstruct all progress; process memory contributes nothing authoritative.
- **Database/server clock drift:** due queries compare `scheduled_for <= clock_timestamp()` in PostgreSQL. Workers may wake early/late but cannot claim before database time. A bounded configurable wake-up tolerance only reduces polling churn.
- **Clock moves backward:** a unique occurrence identity prevents replay; future due selection waits for database time to reach the stored UTC instant.
- **Clock moves forward:** elapsed slots are processed only through the configured bounded misfire policy.

## Lifecycle states

Proposed schedule states are `DRAFT`, `ACTIVE`, `PAUSED`, `COMPLETED` and `CANCELLED`.

- `DRAFT -> ACTIVE | CANCELLED`
- `ACTIVE -> PAUSED | COMPLETED | CANCELLED`
- `PAUSED -> ACTIVE | CANCELLED`
- `COMPLETED` and `CANCELLED` are absorbing.

`COMPLETED` means a one-time schedule has reached a terminal occurrence or a recurring schedule has crossed its end/maximum-occurrence boundary. It says nothing about completion of a dispatched P14 run.

Proposed occurrence states are `PENDING`, `CLAIMED`, `DISPATCHED`, `FAILED`, `SKIPPED` and `CANCELLED`.

- `PENDING -> CLAIMED | SKIPPED | CANCELLED`
- `CLAIMED -> DISPATCHED | FAILED`
- `DISPATCHED`, `FAILED`, `SKIPPED` and `CANCELLED` are terminal for P15.

`DISPATCHED` means P14 accepted the stable start identity and returned the logical `workflow_run_id`; it does not mean that the P14 workflow completed. A claim that becomes ambiguous remains `CLAIMED` with durable dispatch metadata for P25 rather than being blindly reassigned.

## Data model proposal

No table or migration is created by this governance change.

### `scheduler_schedules`

- UUIDv7 primary key and `organization_id` tenant owner.
- Unique `(organization_id, schedule_key)` logical identity.
- Closed `target_type=START_WORKFLOW` and tenant-aware composite FK to immutable `workflow_versions(organization_id, id)`.
- Schedule type, structured recurrence JSON, IANA timezone, UTC `start_at`, optional `end_at`, UTC `next_fire_at`, state, misfire policy, maximum catch-up, revision, timezone-data version, created/updated timestamps.
- Mutable only through optimistic `revision` compare-and-set; terminal states absorb.
- Forced RLS; indexes on `(organization_id, state, next_fire_at)` and tenant/key.
- Runtime hard delete prohibited; archive/retention is a future governed policy.

### `scheduler_occurrences`

- UUIDv7 primary key and `organization_id` tenant owner.
- Tenant-aware composite FK to the schedule and P14 workflow version.
- Snapshot of schedule revision, target, intended local slot, timezone, offset/fold, timezone-data version, UTC `scheduled_for`, misfire policy and canonical `occurrence_key`.
- Unique `(organization_id, schedule_id, occurrence_key)` prevents duplicate materialization.
- State, owner UUID, opaque claim token, claimed time, dispatch-start time, bounded dispatch-attempt count, P14 idempotency key, optional `workflow_run_id`, safe error code, created/updated timestamps.
- Snapshot fields immutable after insertion; claim/result fields mutable only through guarded transitions.
- Forced RLS; due/claim index on `(organization_id, state, scheduled_for)` and lookup index on workflow run.
- Runtime hard delete prohibited.

### `scheduler_transition_history`

- UUIDv7 primary key, `organization_id`, tenant-aware schedule FK and optional occurrence FK.
- Append-only entity type/id, from/to state, reason code, source, correlation ID and timestamp.
- Forced RLS; ordered tenant/schedule/time index; runtime update/delete prohibited.

## Occurrence materialization

P15 uses **just-in-time materialization from durable `next_fire_at`** rather than pre-generating a horizon. A worker locks one eligible active schedule, inserts exactly one occurrence for its current canonical local slot, and advances `next_fire_at` to the next valid slot in the same transaction. The unique schedule/occurrence key is the final duplicate backstop.

The occurrence key is derived from stable non-secret components: schedule UUID, schedule revision and canonical intended local slot (including timezone identity, but not merely the UTC offset). The stored occurrence UUID is opaque externally. A bounded tick processes at most 100 schedules/occurrences by default and never more than a configured hard ceiling of 500.

Schedule edits create a new revision and recompute only future, non-materialized slots. Materialized occurrences retain their revision snapshot. Edit versus materialize serializes on the schedule row so the occurrence records exactly one revision.

## Misfire policy

`MisfirePolicy` is explicit and closed:

- `SKIP`: materialize each elapsed slot only as terminal `SKIPPED` evidence up to the bounded scan limit, coalescing older excess into one summarized transition; dispatch no missed workflow.
- `FIRE_ONCE`: materialize one dispatchable occurrence representing the latest eligible elapsed slot, record how many prior slots were coalesced, then continue from the first future slot.
- `CATCH_UP_BOUNDED`: materialize at most `max_catch_up` dispatchable missed occurrences, where the configured value is 1–100, then record excess slots as bounded/coalesced skips.

There is no implicit unbounded catch-up. `FIRE_ONCE` is the proposed safe default for recurring schedules; `ONE_TIME` defaults to `FIRE_ONCE` until its explicit end boundary. Resume after downtime applies the stored policy under the schedule-row lock before calculating the next future slot.

## Idempotency and P14 dispatch

The P14 idempotency key is stable for the logical occurrence, proposed as `schedule:<schedule_uuid>:<occurrence_uuid>`. It does not change across scheduler dispatch retries or worker restarts. P15 stores the key before invoking P14.

Dispatch sequence:

1. lock the due `PENDING` occurrence under its schedule;
2. verify schedule `ACTIVE`, database time due, current revision snapshot valid and target same-tenant;
3. transition to `CLAIMED`, persist owner/token/attempt and commit;
4. call P14 `start_run` with the immutable workflow version, bounded input reference/snapshot and stable P14 idempotency key;
5. lock the occurrence and compare state, owner and token;
6. persist returned `workflow_run_id` and transition to `DISPATCHED`.

If P14 starts the run but the P15 terminal write is lost, retrying with the same key returns the same logical run. The occurrence remains visibly ambiguous until that replay succeeds or P25 reconciles it. P15 does not claim physical exactly-once execution.

## P14 retry-time boundary

P14 owns step retry classification, attempt ceiling, current step state and eligibility. Current P14 code persists `next_eligible_at` and reclaims a retryable step through P14's own claim path; it has no separate scheduler callback contract. P15 therefore does **not** mutate P14 step rows, calculate P14 retry policy, create duplicate retry occurrences or become retry authority in the initial implementation.

During P15 implementation, the integration subtask must inspect whether P14 needs only ordinary worker polling of its own durable eligibility or a separately governed temporal wake signal. If a wake signal is required, P15 may notify a typed P14 boundary, but P14 must re-check eligibility and retain final authority. Any new cross-phase API requires an explicit contract and tests; it cannot be inferred or silently added.

## Pause, resume and cancellation

- **Pause schedule:** `ACTIVE -> PAUSED` under the schedule-row lock. It prevents new materialization and new claims after commit. A claim that committed first remains an explicit in-flight occurrence.
- **Resume schedule:** only `PAUSED -> ACTIVE`; recompute the temporal cursor transactionally and apply the stored misfire policy to elapsed slots. No hidden backlog is created.
- **Cancel schedule:** terminally prevents future materialization and cancels existing `PENDING` occurrences. It does not erase history.
- **Cancel occurrence:** allowed only while `PENDING`; a committed `CLAIMED` or `DISPATCHED` occurrence cannot be presented as undone.
- **Cancel dispatched P14 workflow:** separate, explicit, authorized P14 cancellation. Cancelling a schedule never implicitly mutates a P14 run. A future API may request both operations explicitly and report independent outcomes.

Claim/cancel and claim/pause races serialize on the schedule row before the occurrence row. Commit order decides; a stale owner/token cannot write a terminal result after authority changes.

## API proposal

Design only, under `/api/v1` and existing bounded pagination/RFC 9457 conventions:

- `POST /schedules`
- `GET /schedules`
- `GET /schedules/{id}`
- `PATCH /schedules/{id}` with expected revision
- `POST /schedules/{id}/activate`
- `POST /schedules/{id}/pause`
- `POST /schedules/{id}/resume`
- `POST /schedules/{id}/cancel`
- `GET /schedules/{id}/occurrences`
- `GET /schedule-occurrences/{id}`
- optional explicit `POST /schedule-occurrences/{id}/cancel` only for `PENDING`

All request models use `extra="forbid"`, bounded payloads and repository pagination. Organization scope comes only from trusted auth context. Requests never contain authoritative `organization_id`, arbitrary code/destination, credentials or provider configuration.

## RBAC proposal

Use the existing resource/action permission architecture:

- `schedule:read`: inspect schedules, occurrences and bounded history;
- `schedule:execute`: activate, pause, resume, cancel schedules and cancel pending occurrences;
- `schedule:configure`: create and revision-edit schedules.

These permissions are design candidates only and are not registered by this governance PR.

## P04 event proposal

Candidate transactional events are `scheduler.schedule.created`, `scheduler.schedule.activated`, `scheduler.schedule.paused`, `scheduler.schedule.resumed`, `scheduler.schedule.cancelled`, `scheduler.occurrence.created`, `scheduler.occurrence.claimed`, `scheduler.occurrence.dispatched`, `scheduler.occurrence.failed` and `scheduler.occurrence.skipped`.

Events contain bounded safe identifiers, schedule revision, scheduled UTC/local facts, state, reason/error code and correlation ID. They never contain credentials, secrets, raw provider authorization, hidden chain-of-thought or unbounded customer/workflow payloads. Event publication uses the existing P04 transactional outbox; broker delivery is not occurrence execution authority.

## Failure matrix

| # | Scenario | Authoritative state | Allowed outcome | Forbidden outcome | Durable protection / deferral |
|---:|---|---|---|---|---|
| 1 | Duplicate schedule create | Tenant/key plus canonical request fingerprint | Identical replay returns one schedule; conflicting payload errors | Second logical schedule | Unique tenant/key and fingerprint |
| 2 | Duplicate occurrence materialization | Schedule revision/local-slot key | Existing occurrence returned | Second occurrence for slot | Unique tenant/schedule/occurrence key |
| 3 | Two workers claim same occurrence | Occurrence state and owner/token | One `PENDING -> CLAIMED` winner | Two dispatch authorities | Schedule/occurrence locks plus CAS |
| 4 | Worker dies before claim | Occurrence remains `PENDING` | Another worker claims | Phantom ownership | No durable token means no authority |
| 5 | Worker dies after claim before P14 | `CLAIMED`, token, dispatch-start null | Remains inspectable/ambiguous | Blind reassignment | Durable claim; automatic recovery deferred P25 |
| 6 | P14 starts but P15 write fails | Stable P14 key; occurrence `CLAIMED` | Replay returns same P14 run and links it | New P14 identity | Stable idempotency; broad reconciliation P25 |
| 7 | P14 rejects payload | Claimed occurrence plus safe error | Bounded classified failure | Provider/tool fallback | Typed P14 contract only |
| 8 | P14 temporarily unavailable | Claim/attempt and retry class | Bounded same-key retry | Infinite retry or new key | Persisted attempt ceiling; stale recovery P25 |
| 9 | Schedule cancelled before claim | Schedule `CANCELLED`; occurrence pending | Pending occurrence cancels | New claim/materialization | Schedule row lock and active predicate |
| 10 | Schedule cancelled after claim | Durable claim precedes cancellation | In-flight dispatch follows explicit ordering; no new work | Pretend effect was undone | Commit order and token; ambiguity P25 |
| 11 | Pause versus claim | Schedule row | One transaction wins | Claim after committed pause | Schedule-first lock order |
| 12 | Resume after long downtime | Paused cursor and policy | Bounded policy application | Unbounded backlog | Misfire ceiling and bounded scan |
| 13 | Misfire `SKIP` | Elapsed slots/cursor | Durable skipped evidence | Dispatch missed run | Policy snapshot and unique slots |
| 14 | Misfire `FIRE_ONCE` | Elapsed slot range | One latest logical dispatch | One dispatch per elapsed slot | Coalesced count and unique key |
| 15 | Bounded catch-up | Policy maximum | At most configured 1–100 dispatches | Excess dispatch storm | Hard batch/catch-up ceilings |
| 16 | DST missing time | Local slot and pinned timezone rules | Terminal skipped slot | Silent time shift | Gap policy and local-slot key |
| 17 | DST duplicated time | Fold metadata | Materialize `fold=0` once | Two logical occurrences | Canonical local-slot uniqueness |
| 18 | Invalid timezone | Validated IANA catalog | Reject create/edit | Fallback to server timezone | Strict validation and pinned tzdb |
| 19 | Workflow version unavailable | Same-tenant immutable FK/lookup | Reject activation or fail occurrence safely | Dispatch another/latest version | Composite FK and immutable target |
| 20 | Wrong-tenant workflow reference | RLS/tenant FK | Not found/constraint failure | Cross-tenant dispatch | Forced RLS and composite FK |
| 21 | Stale owner completion | Current owner/token | Reject stale result | Overwrite current occurrence | CAS predicate; reassignment P25 |
| 22 | Duplicate scheduler process | PostgreSQL due set | Safe contention | Duplicate authority | `SKIP LOCKED`, uniqueness and token |
| 23 | Database reconnect | Durable rows/cursor | Resume polling and reconstruct | Trust lost local timers | PostgreSQL source of truth |
| 24 | Clock moves backward | Stored UTC slot and DB clock | Wait; never replay stored key | Second dispatch | Database time and occurrence uniqueness |
| 25 | Clock jumps forward | Elapsed durable cursor | Apply bounded misfire policy | Unbounded catch-up | Search and batch ceilings |
| 26 | Recurrence calculation overflow | Bounded calculator | Stable exhaustion/validation error | Infinite CPU loop/date overflow | Five-year/candidate hard limit |
| 27 | Schedule end boundary | Exclusive end/max occurrence | Complete schedule | Materialize beyond boundary | Locked cursor calculation and checks |
| 28 | Dispatched occurrence replay | Terminal occurrence and P14 run/key | Return existing linkage | New claim or workflow identity | Terminal absorption and stable key |

## Concurrency matrix

| # | Race | PostgreSQL serialization/fencing | Deterministic result |
|---:|---|---|---|
| 1 | Create same schedule key | Unique `(organization_id, schedule_key)` plus request fingerprint | One create; identical replay or conflict |
| 2 | Materialize same occurrence | Schedule `FOR UPDATE` plus unique occurrence key | One insert and one cursor advance |
| 3 | Claim same due occurrence | Schedule-first lock; occurrence `FOR UPDATE SKIP LOCKED`; state CAS | One owner/token; loser cannot dispatch |
| 4 | Dispatch versus cancel | Lock schedule then occurrence; active/state/token predicates | Commit order decides; cancellation winner fences dispatch |
| 5 | Dispatch versus pause | Same lock order; claim requires schedule `ACTIVE` | Pause winner blocks new claim; prior claim stays explicit |
| 6 | Edit versus materialize | Schedule `FOR UPDATE` and expected revision | Occurrence snapshots exactly old or new revision |
| 7 | Resume versus scheduler tick | Schedule row transition/cursor update under one lock | Resume policy/cursor commits once before eligibility |
| 8 | Stale owner result | CAS on `CLAIMED`, owner UUID and opaque token | Obsolete owner cannot write `DISPATCHED/FAILED` |
| 9 | Duplicate P14 start | Stable occurrence-derived P14 idempotency key | P14 returns one logical workflow run |

No race depends on `asyncio.Lock`, a singleton process, local timer ordering or broker delivery.

## Security red-team answers

1. **Can a user schedule arbitrary shell?** No; `START_WORKFLOW` is the only target and shell is not a contract value.
2. **Can a user schedule arbitrary SQL?** No; there is no SQL job type or query payload.
3. **Can a user schedule an arbitrary URL?** No; no URL/destination field exists and P15 has no network executor.
4. **Can P15 bypass P14?** No; every occurrence references a P14 immutable workflow version and invokes only P14 start.
5. **Can tenant A schedule tenant B's workflow?** No; trusted tenant context, forced RLS and tenant-aware composite FK enforcement fail closed.
6. **Can two replicas dispatch the same occurrence?** They may contend, but only one current PostgreSQL owner/token may dispatch; P14 also deduplicates the stable logical start identity.
7. **Can a stale worker dispatch after cancel?** It cannot gain new authority after committed cancellation. If its claim committed first, the ambiguity is durable and not falsely described as undone.
8. **Can recurrence create an infinite backlog?** No; recurrence search, batch size and catch-up are hard-bounded, and every misfire policy coalesces/skips excess.
9. **Can DST create two occurrences?** No; the canonical intended local-slot key and `fold=0` policy create one logical occurrence.
10. **Can an edit alter a claimed occurrence?** No; occurrences snapshot immutable schedule revision and temporal/target facts.
11. **Can replay create another P14 run?** No; all retries reuse the persisted occurrence-derived P14 idempotency key.
12. **Can scheduler credentials reach an LLM?** No; P15 stores no provider credentials and never invokes P13/provider code directly. P14 governs any later AGENT step through P13.

## Certification and implementation entry

The eventual P15 claim is **SCHEDULER CONTRACT + DURABILITY CERTIFIED**. It is not global physical exactly-once execution and not full disaster/failover certification. P25 retains broad crash recovery and reconciliation.

Implementation may begin only after this governance PR is human-merged, exact canonical `main` is green, the repository still selects P15, and `make nxs-start PHASE=NXS-P15 ACTOR=<agent>` succeeds on `feat/nxs-p15-scheduler`. This governance task adds no scheduler source, migration, API, permission, phase manifest, evidence or execution lock.
