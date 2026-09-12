# NXS-P14 Workflow Engine Pre-Implementation Design

Status: governance proposal; implementation is not authorized by this document. The canonical requirement remains `NXS-WF-001`. NXS-P14 depends on the validated NXS-P08 Tool Engine and NXS-P13 Agent Runtime and publishes through the validated NXS-P04 event boundary.

## Ownership boundary

NXS-P14 owns workflow definitions, immutable published versions, runs, versioned step definitions, step runs, DAG validation/execution, deterministic state transitions, durable progress, tenant isolation, PostgreSQL-authoritative claims, workflow- and step-level idempotency, bounded retry policy and metadata, pause/resume/cancel, failure propagation, condition evaluation, audit-friendly transition history, correlation identifiers, workflow/step events through P04, and API contracts for workflow management and execution.

`TOOL` steps invoke only NXS-P08, which alone reaches NXS-P07 and external services. `AGENT` steps invoke only NXS-P13, which alone reaches model-provider abstractions. P14 never accepts an arbitrary network destination or invokes vendor adapters directly.

P14 does not own cron, recurring schedules, future-time scheduling, or timer services (P15); campaigns, audiences, or bulk outreach (P16); human queues, assignment, or workforce routing (P17); cell placement/scaling (P18); autonomous SRE (P20); billing/cost accounting (P23); the full observability platform (P24); orphan reaping, automatic crash recovery, stale-lease reassignment, ambiguous-effect reconciliation, or failover semantics (P25); or deployment (P32).

## Architectural invariants

- **INV-WF-001 — Tenant isolation.** Every definition, version, version step, run, step run, and transition record has exactly one `organization_id`. Forced RLS and tenant-aware composite foreign keys enforce same-tenant references in PostgreSQL.
- **INV-WF-002 — Durable source of truth.** PostgreSQL, never process memory, is authoritative for workflow/step state, ownership, retry count, cancellation, and idempotency.
- **INV-WF-003 — Immutable execution version.** Each run binds permanently to one published workflow version. Draft edits and later publications cannot alter its graph.
- **INV-WF-004 — Terminal absorption.** `COMPLETED`, `FAILED`, and `CANCELLED` are absorbing workflow states. Step terminal states are likewise non-resurrectable.
- **INV-WF-005 — Distributed claim ownership.** Multiple independent workers are assumed. Row locks, conditional updates, uniqueness, and opaque ownership tokens provide authority; `asyncio.Lock` is never a correctness primitive.
- **INV-WF-006 — P08 tool authority.** The only external action path is Workflow → P08 Tool Engine → P07 Integration Hub → external service.
- **INV-WF-007 — P13 agent authority.** The only AI reasoning path is Workflow → P13 Agent Runtime → model-provider abstraction.
- **INV-WF-008 — Idempotency.** A tenant-scoped workflow-start key identifies one logical run, and a step/attempt identity identifies one authorized semantic execution. Replays return or conflict with durable state rather than duplicating work.
- **INV-WF-009 — Cancel fence.** Once cancellation commits under the workflow-run serialization boundary, no new step claim can commit. Previously authorized attempts follow explicitly recorded completion semantics.
- **INV-WF-010 — Bounded retries.** Retry class, attempt count, ceiling, and next-eligible timestamp are persisted. P14 stores eligibility metadata but does not build P15 scheduling infrastructure.
- **INV-WF-011 — Reconstructible state.** Every material transition has durable before/after state, actor/owner, correlation, time, and reason/code. No critical progress exists only inside a Python task.
- **INV-WF-012 — Crash-recovery boundary.** P14 persists claim token, claimant, claim/heartbeat/expiry metadata and ambiguous outcome state sufficient to detect stale or incomplete execution. P25 owns automatic reconciliation, reaping, reassignment, and failover.

Candidate workflow states are `PENDING`, `RUNNING`, `PAUSED`, `COMPLETED`, `FAILED`, and `CANCELLED`; candidate step states are `PENDING`, `READY`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED`, and `SKIPPED`. Implementation must reconcile these names with established repository enum and transition conventions before freezing contracts.

## Failure matrix

| Scenario | Authoritative state | Allowed transition | Forbidden transition | Durable protection | Deferred |
|---|---|---|---|---|---|
| Duplicate workflow start | Tenant + definition/version + idempotency key | Replay same request; conflict on different payload | Second logical run | Unique tenant-scoped key plus request hash | — |
| Duplicate step claim | Step-run row and claim token | One `READY→RUNNING` winner | Two active owners | `FOR UPDATE` or conditional update with unique attempt | — |
| Two workers claim same step | Step state/version | Single compare-and-set winner | Both dispatching | State/version predicate returning one row | — |
| Crash before claim | Step remains `READY` | Another worker claims | Recording execution without claim | No durable claim means no authority | — |
| Crash after claim, before side effect | `RUNNING` claim with no dispatch permit/result | Preserve ambiguity for inspection | Immediate blind redispatch | Claim/permit timestamps and ownership token | P25 recovery |
| Crash during P08 | Durable P08 idempotency identity and running attempt | Record bounded retry eligibility after classified outcome | New semantic action identity | P08 idempotency plus attempt record | P25 ambiguous reconciliation |
| Effect succeeds, completion persistence fails | P08 result/idempotency record plus incomplete local attempt | Reconcile by same P08 key | Execute with a new key | Stable action key and ambiguous outcome | P25 reconciliation |
| Crash during P13 | P13 session/turn identity plus running attempt | Re-query/replay governed P13 identity | Direct provider retry | Stable agent invocation identity | P25 reconciliation |
| Cancellation before claim | Run `CANCELLED` under lock | Pending/ready steps cancel or skip | New claim | Run lock + claim predicate requiring non-cancelled state | — |
| Cancellation after claim | Claim precedes cancel in durable order | Apply defined in-flight completion rule | New sibling claim | Serialized run transition and claim token | — |
| Pause during active step | Run `PAUSED`, active claim retained | Active attempt completes under policy; no new claims | Claiming another step | Claim predicate requires `RUNNING` | — |
| Resume after pause | Run `PAUSED` | `PAUSED→RUNNING` if nonterminal | Resume after cancel/failure/completion | Conditional transition predicate | — |
| Retryable failure | Failed attempt with retry class/count | Create next bounded eligibility | Exceed ceiling or immediate unrecorded loop | Persisted attempt number and ceiling | P15 wakes future time |
| Non-retryable failure | Failed attempt classified terminal | Propagate step/run failure | Retry | Closed error taxonomy and transition constraint | — |
| Invalid graph | Draft/version graph | Reject publication | Start invalid version | Publish-time validation transaction | — |
| Cyclic graph | Draft edges | Reject publication | Publish/start cyclic version | DAG cycle validation plus immutable snapshot | — |
| Missing dependency | Version step reference | Reject publication | Publish dangling edge | Same-tenant composite FK and graph validation | — |
| Branch evaluated twice | Condition step result/version | Idempotent replay of durable outcome | Contradictory branch choice | Unique condition outcome and compare-and-set | — |
| Tenant mismatch | RLS-scoped rows | Deny/not found | Cross-tenant reference or mutation | Forced RLS + tenant composite FKs | — |
| Stale worker | Ownership token no longer current | Reject stale completion | Mutate current attempt/run | Token/version predicate | P25 reassignment |
| Terminal run execute/resume | Absorbing terminal state | Idempotent terminal response or conflict | Transition to active | Database transition constraint/predicate | — |
| Definition edited during active run | Run points to immutable version | Edit draft/publish new version | Mutate bound version graph | Immutable published rows and FK | — |

## Concurrency matrix

| Race | PostgreSQL serialization and fencing rule | Winner/loser behavior |
|---|---|---|
| Start same idempotency key | Unique `(organization_id, idempotency_key)` plus canonical request hash | One insert; identical loser replays, different loser conflicts |
| Claim same `READY` step | Conditional `UPDATE ... WHERE state='READY' AND version=:expected RETURNING` under run/step lock | One ownership token issued; loser performs no effect |
| Finish same step | Compare-and-set on `RUNNING` plus matching ownership token and attempt | First valid completion commits; duplicate/stale completion is idempotent or rejected |
| Fail same step | Same token/version predicate as completion | One failure transition; later outcome cannot overwrite it |
| Cancel vs claim | Lock workflow-run row first; claim predicate requires `RUNNING` and unchanged run version | Commit order decides; cancellation winner fences new claim |
| Pause vs claim | Same run-row lock order and active-state predicate | Pause winner blocks claim; earlier claim remains explicitly in flight |
| Resume vs cancel | Conditional state transition under `FOR UPDATE` | Cancellation is absorbing; resume can win only from still-`PAUSED` |
| Branch vs duplicate branch | Unique result per condition step run plus ownership token | One durable branch choice; loser reads it |
| Parent completion vs child readiness | Parent transition and child readiness update share one transaction; child predicate checks all required parents | Child becomes ready once; partial parent state cannot expose readiness |

All services acquire locks in the documented run-before-step order to avoid deadlocks. Process-local locks may reduce contention only; they confer no authority.

## Data model proposal

| Entity | Identity and tenant links | Immutability/state | Constraints and indexes | Deletion and idempotency |
|---|---|---|---|---|
| `workflow_definitions` | UUIDv7 PK; `organization_id`; tenant composite identity | Mutable draft metadata and lifecycle; published versions separate | Unique tenant + canonical key; RLS; indexes by tenant/status | Soft archive; never cascades active history; create key may be idempotent |
| `workflow_versions` | UUIDv7 PK; tenant-aware FK to definition; organization included | Published graph metadata/content hash immutable | Unique tenant + definition + version number/content hash; RLS | Restrict deletion when referenced; publish key/hash identifies replay |
| `workflow_version_steps` | UUIDv7 PK; tenant-aware FK to version; same-tenant dependency edges | Type/config/input contract immutable after publication | Unique step key per version; composite FKs; indexes on version and dependencies; RLS | Cascade only with unpublished draft version; no arbitrary executable payload |
| `workflow_runs` | UUIDv7 PK; tenant-aware FK to immutable version | Mutable state/version, cancel/pause metadata, timestamps | Unique tenant + start idempotency key; state/version and eligibility indexes; RLS | Retained/audited; terminal records not hard-deleted; request hash validates replay |
| `workflow_step_runs` | UUIDv7 PK; tenant-aware FKs to run and version step | Mutable explicit state, attempt count, result/error references, eligibility | Unique logical step per run and unique attempt identity; ready/owner indexes; RLS | Retained with run; semantic action key stable across retries where required |
| `workflow_transition_history` | UUIDv7 PK; tenant-aware FK to run and optional step run | Append-only before/after state, code, actor/owner, correlation, timestamp | Ordered run sequence unique; tenant/run/time indexes; RLS | No update/delete through runtime role; event/outbox correlation deduplicates publication |

A separate claim table is not initially justified: `workflow_step_runs` can hold an opaque ownership token, claimant, claim timestamp, heartbeat/expiry metadata, and monotonic row version. If attempts require immutable per-attempt history, introduce a child `workflow_step_attempts`/permit table with unique `(organization_id, step_run_id, attempt_number)`; it must not imply automatic recovery before P25.

Every tenant reference uses `(organization_id, referenced_id)` composite foreign keys, every tenant table has enabled and forced RLS, and runtime access uses the non-bypass role. Published versions and transition history use restrict/retention semantics rather than destructive cascades.

## Step types

- `TOOL`: server-resolved registered P08 tool/action reference and validated input contract only.
- `AGENT`: server-resolved P13 agent definition/session policy and bounded input contract only.
- `CONDITION`: deterministic, validated expression over bounded prior outputs; one durable result selects declared edges.
- `NOOP`: optional deterministic graph/join marker with no network, filesystem, database-query, code-evaluation, or provider authority. It cannot serve as an extension escape hatch.

`SHELL`, `SQL`, `ARBITRARY_HTTP`, `CRON`, `CAMPAIGN`, and `HUMAN_QUEUE` are rejected. Scheduling belongs to P15, campaigns to P16, and human queues to P17.

## API proposal

Under the existing `/api/v1` conventions: create/list/get definitions, patch drafts, and publish a version; start/list/get runs; pause/resume/cancel runs; list logical steps, step attempts, and—if authorization and response bounds allow—transition history. Commands require idempotency keys where replay matters and optimistic concurrency/version preconditions for mutation. Pagination is cursor-based and bounded.

Every request model uses Pydantic `extra="forbid"`. Responses use established UUIDv7, timestamp, pagination, correlation, and RFC 9457 Problem Details conventions with stable `NXS_WORKFLOW_*` codes. No endpoint accepts `organization_id` as authority, arbitrary code, raw URLs, credentials, or provider configuration.

## Event proposal

Candidate P04 events are `workflow.definition.created`, `workflow.version.published`, `workflow.run.started`, `workflow.run.paused`, `workflow.run.resumed`, `workflow.run.completed`, `workflow.run.failed`, `workflow.run.cancelled`, `workflow.step.ready`, `workflow.step.started`, `workflow.step.completed`, `workflow.step.failed`, and `workflow.step.skipped`.

Events use the P04 transactional outbox/idempotency conventions, tenant and correlation identifiers, stable entity/version identifiers, state and safe error codes. They contain no credentials, secrets, raw provider authorization, unrestricted input/output bodies, or hidden model reasoning.

## Security and RBAC proposal

Repository convention uses resource/action permissions such as `tool:read` and `tool:invoke`. Proposed permissions are `workflow:read`, `workflow:execute`, and `workflow:configure`; they are design candidates only and are not added in this governance change. Trusted organization context, not request payload, scopes authorization and RLS. Configuration permission controls drafts/publication; execute controls run commands; read controls bounded inspection.

## Red-team answers

1. **Can two replicas execute the same step?** They may contend, but only one receives the PostgreSQL ownership token and dispatch permit. A loser cannot call P08/P13.
2. **Can a stale worker execute after cancel?** It cannot begin a new effect: every dispatch checks durable run state and current token. Post-permit ambiguity is recorded for P25 reconciliation.
3. **Can an edited definition mutate an active run?** No. The run references an immutable published version; edits create or change a draft/new version.
4. **Can workflow bypass P08?** No. `TOOL` is a typed P08 reference; arbitrary HTTP and vendor adapters are prohibited.
5. **Can an `AGENT` step bypass P13?** No. It references only the P13 runtime boundary and never a provider adapter.
6. **Can tenant A reference tenant B's run/version?** No. Forced RLS and tenant-aware composite foreign keys reject the reference at PostgreSQL.
7. **Can retry duplicate side effects?** P14 reuses a deterministic semantic P08 idempotency identity. Exactly-once effects are not overclaimed; ambiguous outcomes are fenced and later reconciled.
8. **Can a branch evaluate twice inconsistently?** No. A unique durable condition result is written once under claim/version fencing and replayed.
9. **Can `PAUSED` return to `RUNNING` after `CANCELLED`?** No. Conditional transitions and terminal absorption make cancellation win permanently.
10. **Can a terminal workflow resurrect?** No. Database transition constraints/predicates make terminal states absorbing.
11. **Can arbitrary shell/SQL/URL be submitted?** No. Those step types are absent and `extra="forbid"` rejects undeclared configuration.
12. **What if an external effect succeeds but persistence fails?** The stable P08 idempotency key and durable attempt/permit make the outcome explicitly ambiguous; P14 does not blindly redispatch.
13. **What is deferred to P25?** Automatic stale-claim/orphan detection loops, reaping, lease reassignment, ambiguous-effect reconciliation, crash recovery, and failover orchestration. P14 only stores and fences the evidence those mechanisms need.

## Implementation entry criteria

Implementation may begin only after this governance change is merged by an authorized human, canonical `main` is green, `make nxs-start PHASE=NXS-P14 ACTOR=<agent>` succeeds on `feat/nxs-p14-workflows`, and the phase manifest maps `NXS-WF-001`. This document creates no implementation evidence and certifies no P14 gate.
