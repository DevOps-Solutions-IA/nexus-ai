# NXS-P18 — Horizontal Cell Scaling: pre-implementation contract

Status: approved contract; implementation in progress on the feature branch, not certified.
This document specifies acceptance obligations, not proof of completed capabilities
or passing runtime tests. See `nxs-p18-implementation-progress.md` for the partial
implementation checkpoint. Baseline: main `49020eacc03ddc4a9c6a399b23244e692d7d96a8`,
P17 READY/GO. See ADR-0099 for the placement-authority decision.

## 1. Governance and canonical inputs

The registered branch is `feat/nxs-p18-cell-scaling`. Branch selection does not
start a phase. Initial repository validation and actual-branch preflight passed
without a P18 manifest: the guard checks registry/branch/dependencies/lock, not
architecture completeness. The earlier `chore/nxs-p18-governance-alignment`
preflight correctly blocked for branch mismatch; it was not bypassed.

The pre-start declarative path is supported by `phase-manifest.schema.json`
(PLANNED, empty evidence, null commits/timestamps), `validate_invariants()`
(validates all present manifests and exact mandatory requirement mappings), and
`scripts.nxs_start.__main__._ensure_manifest()` (preserves an existing manifest).
There is no separate manifest-generation CLI. This manifest is schema-authored
using that existing-manifest path and the generator's unchanged `_DEFAULT_GATES`;
neither `start()` nor a state transition nor lock acquisition is invoked.
Unsupported schema fields are not added: detailed scope, invariants, acceptance
and evidence expectations live here, referenced by the manifest objective.
The legacy key `requirements_implemented` is the required phase mapping, not a
claim of completion while PLANNED. No implementation evidence is added.

Dependencies remain P13 and P17, both READY/GO. NXS-SCALE-001 retains its ID,
title, mandatory flag, target P18 and dependency NXS-PLATFORM-002. Its description
is refined to make explicit ownership testable, not broadened into later phases.
Tenancy, P04, P13 and P17 contracts are preserved prerequisites, not new phases.
No registry/project-state/readiness/lock mutation is needed for this task.
READY_TO_EXECUTE is a legal lifecycle state but not required for governance;
the separately authorized implementation start will perform its normal sequence.

Relevant precedents: ADR-0009/0017 (Cells/stateless compute), ADR-0024/0026/0028
(PostgreSQL/migrations/lifecycle), ADR-0029 through 0036 (tenancy), ADR-0047 through
0049 (events), ADR-0064 (tool idempotency), ADR-0073 (messaging idempotency),
ADR-0090/0094 (agent execution), ADR-0095/0096/0097 (workflow/scheduler/campaign),
ADR-0098 and the P17 corrective #1 design (human authority and first response).

## 2. Purpose and closed scope

```text
NXS Control Plane -> NXS Cell Manager -> Cell A / Cell B / Cell C / ...
Organization -> one authoritative placement -> Cell compute
```

P18 defines placement, not tenant identity or business execution ownership.
Adding registered Cells permits explicitly placing new Organizations on more
compute. No automatic balancing, health-based placement or capacity certification
is implied. P18 uses the existing PostgreSQL transactional authority domain;
per-Cell database sharding or cross-database consensus is not part of this phase.

Future P18 owns a durable Cell inventory, initial Organization placement,
administrative placement suspension/reactivation on the SAME Cell, deterministic
resolution, admission fencing, bounded administrative inspection, idempotent
mutations, placement history and P04 assignment events.

**Cross-Cell reassignment is deliberately rejected in P18**, including an offline
or apparently idle Organization. A generation increment alone cannot prove that
all previously authorized external effects, workflows, sessions and handoffs have
drained. A later separately governed relocation contract must prove quiescence,
data continuity and outstanding-permit handling; automated recovery belongs to
P25. P18 must not silently implement relocation as an ordinary UPDATE cell_id.

## 3. Identity and durable model obligations (no DDL)

- Cell: immutable server-generated UUID following repository UUID conventions,
  unique bounded normalized key and bounded safe descriptive metadata. A Cell is
  a compute placement identity, not a host address, provider URL, tenant or secret.
  Registration requires explicit platform control authority; tenant RBAC does not
  implicitly authorize inventory mutation. No public arbitrary destination field.
- Minimal inventory state: REGISTERED, RETIRED. Retirement is absorbing, serialized
  with placement creation and refused while any Organization remains bound.
  These are administrative facts, not health/availability certifications.
- Organization placement: one durable current row per `organization_id`, FK to
  Organization and registered Cell, positive monotonic `assignment_generation`,
  state ACTIVE or SUSPENDED, timestamps and mutation identity. Database uniqueness
  on Organization is the final authority even for concurrent first insert.
  `cell_id` is immutable after initial placement in P18. No runtime deletion or
  generation reset; history is append-only and distinct from the current row.
- Tenant-owned mutation receipts/history carry `organization_id`, exact placement
  identity, expected/result generation, actor, reason, correlation ID, UTC database
  timestamp and bounded fingerprint. Tenant references use composite FKs.
- Cell inventory is explicitly platform-global and contains NO tenant membership,
  credentials or tenant content. This classification is not an RLS exemption for
  assignment/history/receipts. Global Cell references use the catalog PK; references
  between tenant-owned entities use `(organization_id, id)` composite integrity.
  Future schema/role review must prove tenant callers cannot mutate the inventory.

## 4. Required invariants

| ID | Obligation |
| --- | --- |
| INV-CELL-001 | Organization and `organization_id` remain the sole tenant identity. All placement-related tenant rows have forced RLS, USING/WITH CHECK, indexes and tenant-aware FKs. |
| INV-CELL-002 | PostgreSQL is the sole durable assignment and generation authority. Runtime role remains `nexus_runtime`, non-superuser/non-bypass; migrations use `nexus_migration`. |
| INV-CELL-003 | An Organization has at most one current placement, hence at most one ACTIVE Cell. Missing does not mean default Cell. |
| INV-CELL-004 | Resolution returns one exact Organization/Cell/generation tuple or a typed failure, never random fallback or fan-out. |
| INV-CELL-005 | Each effective suspend/reactivate mutation increments generation atomically. Old generation never becomes valid again; replay does not increment it. |
| INV-CELL-006 | Placement eligibility and domain admission/claim/authorization are checked under the same PostgreSQL serialization boundary, not read-check-commit-call without fencing. |
| INV-CELL-007 | P18 generation is additional placement context, never a substitute for P13 owners/permits or P17 assignment tokens/conversation generations. |
| INV-CELL-008 | Semantic execution identities survive retries and routing resolution. A Cell or generation change must not mint another workflow/occurrence/message/agent identity. |
| INV-CELL-009 | Assignment mutation, receipt, history and tenant P04 outbox intent commit or roll back together. Cache/event state cannot grant authority. |
| INV-CELL-010 | Previously authorized external effects cannot be physically revoked by suspension. No new authorization after suspension wins. Ambiguity stays durable and does not trigger redispatch. |
| INV-CELL-011 | Control mutations, lists, retries and worker inspection are bounded; no unbounded fleet transaction or process-local correctness lock. |
| INV-CELL-012 | No physical exactly-once, capacity, failover, data-migration or production-readiness claim follows from placement certification. |

## 5. Assignment lifecycle and idempotency

ABSENT -> ACTIVE at initial assignment, generation 1. ACTIVE -> SUSPENDED and
SUSPENDED -> ACTIVE only on the same Cell, using expected generation and explicit
authorization/reason. Every effective change increments generation. Repetition
with the same semantic idempotency key returns its stored historical result,
not newly usable execution authority. Repeating a transition under a different
key with the wrong state/generation is a conflict. Refresh resolution separately.

Control identity is Organization + operation + bounded caller key, with a
canonical fingerprint over target Cell, expected generation and semantic input.
Same key/equal fingerprint returns the same result; changed fingerprint conflicts.
Database uniqueness and transaction receipt, not Python hash() or cache, enforce
deduplication. Cell registration has an analogous platform-scoped key with no
tenant payload. Keys/fingerprints are not credentials.

Cell loss, worker death or PostgreSQL unavailability does not assign another Cell.
Suspension is not automatic failover; reactivation does not reap/reassign existing
domain claims. An old domain token remains subject to its subsystem's independent
rules even after a fresh placement resolution.

## 6. One resolver and one admission boundary

The platform owns one typed placement resolver/admission boundary, consumed by
ingress and background execution adapters; subsystems must not implement their
own routing tables. First authenticate and resolve trusted Organization context;
then resolve placement. Bootstrap/authentication needed to establish tenant
context is not routed using an untrusted `cell_id` header.

Resolution reads authoritative current state and returns Organization, Cell and
assignment generation. It is a snapshot, NOT a reusable execution permit.
Concurrent reads before/after a committed mutation may legitimately see different
generations. The later admission transaction must validate the exact tuple and
ACTIVE state before a domain claim or authorization can commit.

Admission verifies the server-configured/authenticated worker Cell identity, not
a client assertion. Worker Cell identity never grants Organization permissions.
The future implementation must provide one reusable guard invoked in the SAME
tenant transaction as each protected domain mutation/permit; a remote resolver
check alone cannot close the TOCTOU window. No DB transaction spans provider I/O.

Proposed error semantics (stable NXS codes finalized with implementation contracts):

| Situation | Required behavior |
| --- | --- |
| Unknown/unauthorized Organization | Existing tenant/auth not-found or denial; no topology disclosure. |
| No assignment | Explicit assignment-required conflict; zero domain dispatch. |
| Suspended assignment | Explicit suspended denial; zero new claim/authorization. |
| Unknown/retired Cell, corrupt/ambiguous mapping | Fail closed with safe control-state error; never pick a fallback. |
| Wrong worker Cell or stale generation | Fenced conflict; bounded fresh resolution allowed, not automatic side-effect replay. |
| Database unreachable | Unavailable; cache cannot authorize offline execution. |
| Forged route/tenant/header or unknown contract version | Reject before domain/provider entry. |

Caches may hint where to connect, but the destination always rechecks PostgreSQL
admission. No TTL, cache invalidation event or JetStream receipt is an authority
lease. Internal transport must authenticate callers, bound forwarding/retries and
reject routing loops; concrete transport is not selected in this governance task.

Existing Organizations require explicit bounded, audited initial placements before
Cell-enforced execution is enabled in a later rollout. No fabricated default
assignment or silent legacy bypass. Migration can add the structures without
dispatching work; rollout/data backfill needs a reviewed implementation plan.

## 7. Serialization and external-effect ordering

Proposed common outer order: referenced Cell catalog rows (ascending Cell ID),
Organization placement authority (ascending Organization ID), then the subsystem's
existing lock order, then receipt/history/outbox writes. Runtime paths take shared
placement locks; placement mutations take exclusive locks. Initial placement
serializes through the Organization row or equivalent unique-insert arbitration
because an absent placement row cannot be locked. Catalog retirement takes an
exclusive catalog lock, while assignment creation validates it under a shared lock.

Never acquire an earlier lock class from inside an already-locked domain call.
Cross-boundary execution must pass the admitted transaction context or finish the
transaction and start a new correctly ordered admission transaction. P17's internal
queue -> presence -> work -> conversation ownership -> assignment -> handoff ->
action-authorization order stays unchanged. Multiple row locks are ascending.

Admission/permit commits first: that exact authorized external action may finish
after suspension. Suspension commits first: new domain authorization is denied.
Historical result persistence may record only the exact already-authorized attempt
under its existing token/result fence; it must not create a new permit, unlock new
work or grant human/AI ownership while placement is suspended. AI-return acceptance
requiring a new AI ownership grant waits for valid placement admission; it is not
misrepresented as granted. Ambiguous results retain the original stable identity
for P25. P18 never substitutes itself for existing safe replay contracts.

## 8. Events and audit

Candidate tenant event types: `cell.assignment.created`,
`cell.assignment.suspended`, `cell.assignment.resumed`. Use the P04 versioned
envelope/registry, tenant outbox in the mutation transaction and existing subject
builder; payload is bounded opaque IDs, old/new state, generation, reason and
correlation only. No addresses, credentials, tenant contents or capacity assertions.

Consumers use P04 transactional receipts and acknowledge only after commit.
Duplicates are harmless; older generations cannot replace newer cache projections.
A future/unsupported event version fails closed. Projection gaps trigger bounded
authoritative reread, not inferred state. No event consumer writes placement
authority based solely on an event. Assignment events remain tenant-scoped even
though Cells are platform objects.

Global inventory registration/retirement has its own durable control history;
P04 global publication is not a transactional tenant outbox. Therefore P18 does
NOT promise atomic global inventory event delivery or make global events a trigger
for authority. If durable global event intent is later required, its contract must
be governed separately, not smuggled into tenant outbox semantics. Existing P04
outbox relay recovery remains unchanged and is not new P18 failover behavior.

## 9. Subsystem compatibility obligations

| Boundary | Preserved semantics and future proof |
| --- | --- |
| P06 customers/conversations | Original tenant IDs, composite references and conversation identity survive routing; no data copied into Cell metadata. |
| P07/P08 integrations/tools | Placement does not grant credentials, tool permission or arbitrary network access. Stable semantic keys and tool dispatch authority remain mandatory. |
| P09 messaging | Existing channel/provider abstractions and message fingerprints/keys remain; inbound duplicates still deduplicate. No direct provider routing from Cell metadata. |
| P10 OTP | Issuance, expiry, replay resistance and throttling remain tenant-scoped; routing cannot reset attempts or resend under a new key. |
| P11/P12 voice | Preserve call/session references and media owner; do not relocate active media or implement SIP/Kamailio routing. |
| P13 Agent Runtime | Keep exact session/turn identity, contract version, execution owner/lease, model/tool permits and cancellation fences. Placement adds a guard, never replaces these or enables ALREADY_EXISTS redispatch. |
| P14 Workflow Engine | Immutable versions, ALL/ANY semantics, run/step tokens, terminal absorption and pause/cancel remain; stale placement cannot create another logical run/step. |
| P15 Scheduler | Database time, occurrence revision/timezone identity, misfire summaries and cursor atomicity remain; no occurrence replay on another Cell with a new P14 key. |
| P16 Campaigns | Preserve exact scheduled-release linkage, one-time campaign scope, consent/suppression epochs, throttle reservations and final AUTHORIZED send-permit linearization point. |
| P17 Human Operations | Preserve queues/capacity/order, tokens/lease versions, authority-shape CHECKs, conversation generations, supervisor actions and two-stage AI return with expected P13 contract/exact binding. No stale Cell may restore ownership. First-response timestamp remains once-only with successful consumed P09 result, not route acceptance. |
| P04/audit producers | Business state and tenant event intent/history remain atomic; cache/event replay never restores an older placement or domain owner. |

## 10. Future concurrency and failure matrix

All rows are **REQUIRED / NOT EXECUTED for P18**, not PASS evidence. Real independent
PostgreSQL sessions, explicit barriers and actual downstream invocation counts are
required; sleep timing and SQLite do not prove these obligations.

| Case | Serialization / expected observable result |
| --- | --- |
| C01 two first assignments, different Cells | Organization arbitration + DB unique Organization; one binding, loser conflict, no second authority. |
| C02 equivalent concurrent request/key | Unique receipt/fingerprint + row lock; same binding/result, one generation/event. |
| C03 same key, changed target or expected generation | Fingerprint conflict, zero additional mutation. |
| C04 concurrent suspend/resume writers | Expected-generation CAS + exclusive placement lock; one wins, stale writer changes zero rows. |
| C05 resolve versus suspension | Snapshot may precede commit; shared admission versus exclusive mutation ensures no new permit when suspension wins. |
| C06 stale cached route after reactivation | Old generation rejected, even on the same Cell; no identity reset. |
| C07 cross-Cell reassignment request | Rejected, including when suspended; old binding unchanged. |
| C08 retirement versus first assignment | Shared/exclusive catalog lock; retired target rejected or retirement blocked by existing binding. |
| C09 transaction rollback/outbox enqueue failure | Assignment, receipt, history and event all absent/unchanged. |
| C10 commit succeeds, response lost | Same mutation key returns recorded result; no second assignment/generation. |
| C11 delayed/duplicate/out-of-order event | Receipt/generation check prevents stale projection; PostgreSQL alone authorizes. |
| C12 cache/NATS failure or database reconnect | Cache/bus loss cannot create authority; DB unavailable fails closed, recovered pooled tenant scope never leaks. |
| C13 permit-first versus suspension-first | Explicit commit order; earlier permit may finish, later permit denied; no physical cancellation claim. |
| C14 worker dies after domain claim/permit | Durable original identifiers remain; no new Cell takeover or redispatch; P25 boundary. |
| C15 wrong tenant, forged Cell or generation | RBAC/RLS/composite FK + admission reject; no foreign data or provider calls. |
| C16 P13/P17 handoff versus stale placement | No model/tool/new human-send authorization or AI ownership grant from stale tuple; domain tokens remain fenced. |
| C17 duplicate workflow/schedule/campaign delivery | Original domain idempotency and tokens decide; route retry creates no new logical work. |
| C18 malformed/unbounded metadata or cursor | Strict bounded contract rejects; no arbitrary URL, shell, SQL, secrets or unbounded scan. |

## 11. Future acceptance and evidence plan

Each criterion below is **REQUIRED, NOT VALIDATED**. Later implementation must map
each to named tests and executed evidence under `.nxs/evidence/NXS-P18/`; that
directory is not created here. The manifest retains all 31 canonical gate names.

1. Repeated authoritative reads return the same Organization/Cell/generation tuple.
2. Database rejects multiple current placements for one Organization (C01).
3. Restart reconstructs placement solely from PostgreSQL, without caches.
4. Raw SQL tenant attacks fail under forced RLS and the non-bypass runtime role.
5. Cross-tenant history/receipt/resource references fail composite FK checks.
6. Two independent assignment workers produce exactly one current binding.
7. Stale control writers change zero rows and receive stable conflicts.
8. Suspend/resume increments generation; stale actors remain fenced after resume.
9. Same-key replay is stable; changed fingerprint conflicts (C02/C03/C10).
10. Invalid/ambiguous/retired placement never fans out or randomly selects a Cell.
11. Missing placement returns explicit failure with zero downstream invocation.
12. Rollback and outbox-failure injection prove state/event atomicity (C09).
13. Duplicate/reordered events and cache outage cannot grant execution authority.
14. P13 contract/version/exact-session, owner and model/tool-permit regressions pass.
15. P17 concurrency, ownership CHECKs, AI-return and first-response regressions pass.
16. Messaging idempotency and permit ordering survive suspension/route retry.
17. Workflow, Scheduler and Campaign identities and corrected misfire/bridge
    semantics survive duplicate delivery; no new logical downstream execution.
18. Tool Engine permissions, credential isolation and P07-only integration remain.
19. C01–C08/C13/C16 races run against real PostgreSQL with deterministic barriers.
20. Adversarial tenant/auth tests cover placement, history, cache and worker context.
21. C09–C18 failure paths prove no unsafe reassignment or ambiguous redispatch.
22. Future additive migrations pass clean upgrade, upgrade from canonical schema,
    Alembic check, forced-RLS guard and isolated downgrade/reupgrade where safe;
    production rollback/roll-forward notes follow engineering standards. No
    destructive rollback or deployed migration rewriting is authorized here.
23. Documentation and evidence make no Organizations-per-Cell or physical
    exactly-once/failover/deployment claim.

Implementation evidence must cover git/preflight, schema/tenancy, assignment and
resolution, concurrency/fencing/idempotency, events, subsystem regressions,
security/red-team, full suite/coverage, Docker/multiarch/clean-room, exact-head
CI/Security and canonical two-commit closure. Governance validators do not satisfy
these implementation obligations. Minimum existing coverage threshold stays 90%.

## 12. Non-scope, readiness and limitations

Excluded: P19 SIP Edge Scaling/Kamailio; P20 Sentinel; P21 full Compliance Controls;
P22 Audit Platform; P23 Metering and Cost; P24 global Observability; P25 automated
recovery/reconciliation/failover; P26 Backup/DR; P27 final Security Hardening;
P28 Capacity Certification; P29 Chaos/Failover Certification; P32 Production
Deployment. Also excluded: frontend, provisioning production infrastructure,
automatic scaling, live data/media relocation and any unverified per-Cell capacity.

Explicit tradeoff: an unavailable assigned Cell may leave work unavailable until
the separately governed recovery capability exists. P18 prioritizes no duplicate
authority over availability; initial placement plus same-Cell suspension is not
live migration. Global inventory administration requires least-privilege review;
Organization authorization must not be inferred from access to inventory metadata.

Governance GO means the design and manifest validate, not P18 READY/GO. Governance
alignment left the phase PLANNED/PENDING, with no lock or implementation timestamps.
A separately authorized implementation must reverify canonical Git/state, start the lifecycle,
implement/test every acceptance criterion, and close with real evidence. No push
or PR is needed to perform these local governance validations; review and exact-head
checks remain mandatory before any later authorized merge.

## 13. Governance-only validation record

Baseline discovery verified a clean tree and unchanged canonical main
`49020eacc03ddc4a9c6a399b23244e692d7d96a8`. Exact-main NXS CI run 35268072924 and
NXS Security run 35268072923 were both completed/success. Neither local nor remote
`feat/nxs-p18-cell-scaling` existed; the branch was created from origin/main.

Executed governance checks (exit 0): `make nxs-validate-repo` -> NXS_REPOSITORY
PASS; `make nxs-preflight PHASE=NXS-P18` -> PASS/AUTHORIZED before and after
manifest authoring; `git diff --check` -> no whitespace errors; `make lint` ->
Ruff format/check pass; `make type` -> no issues in 305 source files.
`uv run pytest -q tests/unit/test_control_core.py tests/unit/test_control_cli.py
--no-cov` -> 25 passed. These are existing control-system tests, not P18 runtime
tests; any lifecycle execution in them is confined to test fixtures, not this
repository's state. The schema/invariant validator functions also passed when
called directly. `uv run python -m scripts.nxs_guard lock status --json` confirmed
RELEASED, no holder and no stale lock.

No runtime, migration or P18 evidence files were added. Project-state, registry,
readiness and lock remain unchanged. README now reflects the already-merged P17
baseline and explicitly labels P18 planned. The earlier wrong-branch NO-GO is
historical, not hidden or relabeled PASS. No preflight override was used.
