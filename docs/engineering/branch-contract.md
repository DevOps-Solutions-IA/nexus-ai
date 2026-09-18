# Branch Contract

Every execution branch must define: phase ID, objective, requirement IDs, dependencies and preconditions; scope and non-scope; functional, security, concurrency, resilience and test criteria; evidence paths; rollback and observability; GO/NO-GO decision; and an objective READY definition.

Every executable phase must map to at least one canonical mandatory requirement in `.nxs/requirements.json` whose `target_phase` equals that phase. The control system enforces this: `scripts/nxs_start` refuses to open a phase with no mandatory requirement, and `.nxs/phase-manifest.schema.json` requires `requirements_implemented` to be non-empty. A registry phase that has no such requirement is a governance gap and cannot be started until one is added canonically.

## NXS-P18 implemented contract and documentation corrective

Historical lineage: governance `de032d55e1425a2076c6f0c9454c32474bac956f`, immutable
runtime `b655e615b18aafec4f7a1cc57e25bd97cf6b0a79`, original READY/GO closure
`c2de25360aff1644528f2ce314e98403cb3fdbf4`. The documentation corrective was reopened
as VALIDATING/PENDING, audited externally, and canonically reclosed READY/GO against
`cfa98bd76c0b0e00653356651d672b4e213ef630`. Runtime is unchanged. Final external
post-reclosure audit and merge remain pending; canonical main remains through P17.
No restart, merge, deployment or P19 start is authorized.

- Registered branch: `feat/nxs-p18-cell-scaling`; dependencies: NXS-P13 and NXS-P17 READY/GO. Requirement: `NXS-SCALE-001`, retaining its NXS-PLATFORM-002 dependency.
- Objective/scope: stateless Cell compute with durable inventory, one PostgreSQL-authoritative Organization placement, deterministic resolution, same-Cell suspend/reactivate, generation fencing, stable mutation identities, history and tenant P04 outbox intent.
- Canonical design: `docs/engineering/nxs-p18-cell-scaling-design.md`; approved authority decision: `docs/adr/0099-cell-placement-authority.md`. The 23 acceptance criteria and 18 race/failure cases have executed mappings in `.nxs/evidence/NXS-P18/execution-criteria.json`; original closure evidence remains historical certification, not a new corrective GO.
- Historical governance preparation used the schema-supported existing-manifest path: PLANNED/PENDING, empty evidence and null commits/timestamps. Those were pre-implementation facts. The corrective used READY → VALIDATING and canonical lock acquisition, not `nxs-start`, before its separately authorized reclosure.
- Invariants: Organization/organization_id remains the tenant; forced RLS, tenant composite FKs and non-bypass runtime role remain mandatory. One common placement-admission boundary serializes with mutations before existing domain locks; it supplements P13/P17 authority rather than replacing it.
- Non-scope: cross-Cell relocation/sharding, automated scaling/failover/reconciliation, frontend, SIP/Kamailio and P19–P29 capabilities, production infrastructure/deployment P32, and unverified capacity claims.
- Evidence/rollback: original implementation and certification evidence exists under `.nxs/evidence/NXS-P18/`, including PostgreSQL race/tenant/failure tests, subsystem regressions and migration checks. The documentation corrective adds separate evidence without rewriting those results; it changes no runtime or database schema.
- Observability: bounded safe placement history/reason codes and tenant assignment events, not a global P24 observability platform.
- Readiness: P18 is READY/GO after externally authorized canonical corrective reclosure. Main has not received P18. Final external post-reclosure audit and explicit merge authorization are still required. No deployment or later-phase activation is authorized.

## NXS-P00 contract

- Phase: NXS-P00; branch: `feat/nxs-p00-engineering-control-system`.
- Objective: permanent agent-neutral governance, evidence, CI, security, deterministic runtime and validation.
- Preconditions: empty private canonical repository, initialized `main`, no prior P00 READY state.
- Scope: manifest requirements and minimal health/version runtime only. Business capabilities and production deployment are excluded.
- Criteria: every mandatory manifest gate passes; malformed and conflicting execution states block; container runs non-root and healthy; an independent agent reconstructs status from repository files.
- Rollback: abandon the unmerged feature branch; `main` retains repository identity only.
- Observability: structured runtime lifecycle logs and health/version endpoints.
- READY: requirements VALIDATED, evidence present, implementation SHA recorded, status READY and decision GO.

## NXS-P01 contract

- Phase: NXS-P01; branch: `feat/nxs-p01-backend-core`; depends on NXS-P00 (READY/GO, merged at `73b399f`).
- Objective: make the engineering lifecycle generic for every registry phase, then establish the permanent production Backend Core.
- Requirements: `NXS-PLATFORM-002`, `NXS-PLATFORM-003`, `NXS-CICD-001`, `NXS-CONFIG-001`, `NXS-API-001`, `NXS-ERROR-001`, `NXS-CTX-001`, `NXS-LOG-001`, `NXS-TELEMETRY-001`, `NXS-HEALTH-001`, `NXS-DATA-001`, `NXS-CACHE-001`, `NXS-EVENT-001`, `NXS-MIGRATE-001`.
- Preconditions: `main` synchronized, guard PASS on the P01 branch, no conflicting active phase.
- Scope: generic phase start/closure/next-phase, executable lock CLI, phase-neutral CI, unified Python 3.14 toolchain, application factory, validated configuration, `/api/v1` foundation, Problem Details errors, request/correlation context, structured redacted logging, telemetry boundary, async PostgreSQL/Valkey/NATS foundations, liveness/readiness, Alembic framework.
- Non-scope: organizations, tenancy, auth/OTP, CRM/ERP, conversations, channels, telephony, ElevenLabs, agent runtime, workflows, campaigns, human agents, cell scaling, production deployment, business tables and event contracts.
- Security: secrets never in `repr`/logs; DSNs/URLs redacted; Authorization and Cookie headers never logged; trusted-host and default-deny CORS; no wildcard CORS in production; no stack traces in responses; SHA-pinned third-party actions; least-privilege workflow permissions.
- Concurrency: request/correlation ids never bleed between concurrent tasks; DB sessions independent; errors stay correlated (proven under 200-way concurrency).
- Failure scenarios: PostgreSQL / Valkey / NATS unavailable → live 200, ready 503, recovery without restart; invalid production configuration → startup refused; unhandled exception → safe Problem Details + correlated internal log.
- Tests: `tests/unit`, `tests/contracts`, `tests/security`, `tests/concurrency`, `tests/resilience`, `tests/integration` (real PostgreSQL/Valkey/NATS); ≥90% branch-aware coverage on active code.
- Evidence: `.nxs/evidence/NXS-P01/`; human summary in `docs/readiness/NXS-P01/`.
- Rollback: abandon the unmerged branch; `main` stays at the certified P00 state. Alembic ships no versions, so there is nothing to roll back; the first migration will carry its own notes.
- Observability: startup/shutdown events logged, dependency state changes visible, errors carry request/correlation ids, secrets redacted, readiness reports dependency state safely.
- READY: P00 still READY/GO; lifecycle generic with no hardcoded P01; toolchain consistent; factory + configuration + API v1 + Problem Details + isolated context + structured logging + PostgreSQL/Valkey/NATS + liveness/readiness + graceful shutdown + non-root image + security gates + clean-room + green GitHub CI all pass; P01 requirements VALIDATED; agent handoff resolves P02 next; project state says P01 READY/GO with `next_allowed_execution = NXS-P02`; no merge to `main`.

## NXS-P02 contract

- Phase: NXS-P02; branch: `feat/nxs-p02-tenancy`; depends on NXS-P01 (READY/GO, merged at `1cc4e28`).
- Objective: the permanent multi-tenant security boundary — Organization core domain + strict tenant isolation enforced by PostgreSQL, not application `WHERE` clauses.
- Requirements: `NXS-TENANT-001`..`NXS-TENANT-005`, `NXS-ORG-002`, `NXS-ORG-003`, `NXS-SEC-003`, `NXS-CICD-002`.
- Preconditions: `main` synchronized at P01; guard PASS on the P02 branch; `NXS-ORG-001` remains PLANNED / target NXS-P05.
- Scope: Organization aggregate (UUIDv7 id, reserved-safe key, validated profile, lifecycle state machine, optimistic concurrency), first real migration, forced RLS, transaction-local tenant context, non-bypass `nexus_runtime` role + `nexus_migration` role, tenant unit of work, `TenantOwnedMixin` + schema-guard CI gate, tenant namespace primitive, `TenantContextResolver` boundary, current-Organization API, multi-arch amd64/arm64.
- Non-scope: authentication/RBAC/users (P03); automatic provisioner, admin, quotas, dashboards, channels, billing (P05 / NXS-ORG-001); customers/conversations (P06); events (P04); audit ledger (P22); cell scaling (P18); capacity claims (P28); deployment / Oracle LAB.
- Security: RLS ENABLED + FORCED; runtime role non-superuser, NOBYPASSRLS, no DELETE on organizations, no DDL; startup fails closed in staging/prod if the role can bypass tenancy; header tenant resolver rejected in staging/prod; `tax_identifier` never in public view/repr/logs; no raw DB errors at the API; Gitleaks/Bandit/pip-audit/Trivy pass.
- Concurrency: 240 mixed A/B/C operations with zero cross-tenant reads/writes and ContextVar isolation.
- Failure/attack scenarios: cross-tenant SELECT/UPDATE/INSERT blocked; DELETE unavailable; no-scope → zero visibility; pool reuse across commit/rollback/exception → no leak; spoofed header / body organization_id → no scope switch; stale optimistic update → deterministic conflict; runtime role privilege-escalation attempts fail.
- Tests: `tests/unit`, `tests/contracts`, `tests/security`, `tests/concurrency`, `tests/resilience`, `tests/integration` (real PostgreSQL as `nexus_runtime` with RLS enforced, `nexus_migration` for schema); ≥90% branch-aware coverage; tenant-boundary modules substantially higher.
- Evidence: `.nxs/evidence/NXS-P02/`; human summary in `docs/readiness/NXS-P02/`.
- Rollback: abandon the unmerged branch; `main` stays certified through P01. The organizations migration is reversible in an isolated database (`downgrade base` → `upgrade head` tested); production migrations carry roll-forward notes per `docs/engineering/standards.md`.
- Observability: startup logs the runtime role's bypass capability; `organization_id` enters the log context only after trusted resolution; no high-cardinality tenant metric labels.
- READY: P00 + P01 still READY/GO; P02 started through generic lifecycle; Organization domain + first migration exist; RLS ENABLED + FORCED; runtime role cannot bypass; missing context fails safe; A cannot read/update/insert-as/delete B; pooled connection and async execution cannot leak scope; spoofed identifiers cannot switch scope; lifecycle + optimistic concurrency enforced; tenant-owned pattern reusable + schema validator catches unsafe tables; P03 and P05 seams intact; amd64 + arm64 images build; full regression + coverage ≥90% + security gates + clean-room + green GitHub CI; P02 requirements VALIDATED; `NXS-ORG-001` still PLANNED for P05; project state says P02 READY/GO with `next_allowed_execution = NXS-P03`; no merge to `main`.

## NXS-P14 contract

- Phase: NXS-P14; implementation branch: `feat/nxs-p14-workflows`; depends on NXS-P08 and NXS-P13, both READY/GO.
- Objective: deliver a durable, tenant-isolated workflow engine that composes governed P08 tool actions and P13 agent turns through an explicit PostgreSQL-authoritative state machine.
- Requirement: `NXS-WF-001`. Preconditions: this governance alignment is merged, canonical `main` is green, and the generic lifecycle starts P14 without a conflicting active phase.
- Scope: immutable versioned DAGs, workflow/step runs, durable claims and progress, bounded retry metadata, pause/resume/cancel, condition branches, idempotency, transition history, P04 events, and bounded workflow APIs. Detailed invariants and matrices are in `docs/engineering/nxs-p14-workflow-engine-design.md`.
- Non-scope: P15 scheduling/timers; P16 campaigns; P17 human routing; P18 placement/scaling; P20 autonomous SRE; P23 billing; P24 full observability; P25 automatic orphan recovery/reassignment/reconciliation/failover; P32 deployment.
- Security/concurrency: forced RLS and tenant composite FKs; typed P08/P13-only execution; no arbitrary HTTP/shell/SQL; PostgreSQL row locks, uniqueness, conditional transitions, and ownership tokens are authoritative across replicas.
- Failure criteria: duplicate starts/claims, cancellation and pause races, worker crashes, ambiguous external effects, graph defects, stale owners, tenant mismatch, retry exhaustion, and terminal resurrection are tested against the pre-implementation matrices.
- Evidence: `.nxs/evidence/NXS-P14/` after commands actually execute. Rollback before merge is branch abandonment/revert; migrations require their own reversible and roll-forward proof.
- Observability: correlated, safe state transitions and P04 events expose workflow/step progress without credentials, raw authorization, unrestricted bodies, or hidden reasoning.
- READY: every P14 gate passes locally and on exact-head GitHub CI/Security; `NXS-WF-001` is VALIDATED with real evidence; P14 is READY/GO; the generic next phase is calculated; no merge or deployment occurs without human authorization.

## NXS-P15 contract

- Phase: NXS-P15; implementation branch: `feat/nxs-p15-scheduler`; depends only on NXS-P14 READY/GO.
- Objective: answer when a governed workflow should start by delivering a durable, tenant-isolated scheduler that dispatches immutable P14 workflow versions and never executes business effects itself.
- Requirement: `NXS-SCHED-001`, which depends on `NXS-WF-001`. Preconditions were satisfied from canonical `main` at `3774ef5318907ae811f1bfff206754ad862cf56f`; the generic lifecycle started P15 on its required implementation branch and acquired the repository execution lock.
- Implementation decision: `feat/nxs-p15-scheduler` implements the approved governance contract without changing P14 retry authority, taking P25 recovery authority, activating P16, or deploying production.
- Scope: one-time and recurring schedules; pinned timezone-aware recurrence; just-in-time durable occurrence materialization; PostgreSQL-authoritative claims and owner/token fencing; stable P14 start idempotency; bounded misfire handling; schedule pause/resume/cancel; optimistic revisions; P04 events; bounded APIs and existing RBAC conventions. Detailed temporal, failure, concurrency, security, and ownership rules are in `docs/engineering/nxs-p15-scheduler-design.md`.
- Non-scope: P16 campaigns/audiences/bulk throttling; P17 human workforce routing; P18/P19 infrastructure placement and SIP scaling; P20 autonomous operations; P23 billing; P24 complete observability; P25 automatic orphan recovery, stale-owner reassignment, ambiguous-dispatch reconciliation, and failover; P26 backup/DR; P32 deployment. Shell, SQL, arbitrary HTTP/Python, host cron, infrastructure schedulers, direct tools, providers, messaging, and telephony are prohibited.
- Security/concurrency: every scheduler table is tenant-owned with forced RLS and tenant-aware composite FKs; trusted context supplies organization scope; the target is an immutable same-tenant P14 workflow version; due work serializes with PostgreSQL row locks, uniqueness, conditional transitions, and opaque owner/token fencing rather than process-local locks.
- Failure criteria: the 28-scenario design matrix covers duplicate creation/materialization/dispatch, multi-worker claims, crash ambiguity, P14 failures, cancellation and pause races, bounded misfires, DST folds/gaps, clock movement, tenant mismatch, stale owners, recurrence limits, and replay.
- Evidence: `.nxs/evidence/NXS-P15/` records only commands actually executed. Rollback before merge is branch abandonment/revert; the P15 migration supports downgrade and clean re-upgrade without unrelated destructive changes.
- Observability: the implementation emits bounded transactional P04 scheduler lifecycle/occurrence events and append-only transitions with safe IDs, timestamps, state, reason and correlation; it emits no credentials, raw authorization, hidden reasoning, or unbounded customer payloads.
- READY: certification is `SCHEDULER CONTRACT + DURABILITY CERTIFIED`, not physical/global exactly-once or disaster-failover certification. It requires all P15 gates, evidence, exact-head GitHub CI/Security, `NXS-SCHED-001` VALIDATED, P15 READY/GO, the next eligible phase computed without activation, and no unauthorized merge/deployment.

## NXS-P16 contract

- Phase: NXS-P16; implementation branch: `feat/nxs-p16-campaigns`; registry dependencies are NXS-P09 and NXS-P15, both READY/GO before implementation starts.
- Objective: deliver durable, tenant-isolated, consent-aware and suppression-aware bulk outreach over the certified WhatsApp, Email and SMS channels without permitting uncontrolled fan-out or a parallel provider path.
- Requirement: `NXS-CAMP-001`, depending on `NXS-WF-001`, `NXS-SCHED-001`, `NXS-WA-001`, `NXS-EMAIL-001` and `NXS-SMS-001`. This governance branch aligns the contract only; it does not start P16 or create a phase manifest.
- Authority chain: one P15 occurrence releases one immutable P14 campaign workflow; P16 materializes and claims recipients in bounded PostgreSQL batches; each recipient receives a stable P14 workflow execution and, only after its governed success, one tenant/attempt-bound durable send permit authorized under the current consent/suppression epochs. The `AUTHORIZED` permit commit is the logical-send linearization point; P16 then calls P09 `MessagingService.send` with its stable key and never calls providers, P08/P07 messaging tools, arbitrary destinations, shell, SQL or infrastructure directly.
- Scope: immutable campaign revisions and audience snapshots; bounded resumable materialization; campaign-specific consent, unsubscribe and suppression enforcement with monotonic policy epochs; IANA quiet hours; PostgreSQL throttle windows, recipient claims and final send permits; durable per-recipient P14/P09 idempotency; lifecycle, pause/resume/cancel fences; P04 events; safe outcomes; campaign RBAC and bounded APIs. The complete ownership, state, data, failure, concurrency and red-team contract is `docs/engineering/nxs-p16-campaigns-design.md`.
- Non-scope: voice campaigns/predictive dialling; P17 human queues; P18/P19 infrastructure and SIP scaling; P20 Sentinel; full P21 compliance; P23 billing; P24 full observability; P25 orphan/failover reconciliation; P26 DR; P32 deployment; frontend work.
- Security/concurrency: forced RLS and tenant-aware composite FKs; trusted tenant context; affirmative channel consent; monotonic consent/suppression epochs; immutable revision/snapshot binding; bounded audience and rate limits; run-first PostgreSQL locks, `SKIP LOCKED`, uniqueness, owner/token compare-and-set and unique send-permit fencing. Consent/suppression/cancel committed before permit authorization blocks it; a permit committed first is already logically authorized and may proceed without holding a database transaction over provider I/O. Valkey/process locks may optimize but never authorize.
- Failure criteria: invalid/cross-tenant/oversized audiences, consent and quiet-hour failures, duplicate P15 wake, multi-worker materialization/claims, pause/cancel/unsubscribe/suppression versus final-permit races, authorized-but-unconsumed P09 failures, accepted-but-unrecorded ambiguity, rate limits, stale owners and outbox failures resolve according to the durable matrices without provider bypass or duplicate logical sends.
- Evidence/rollback: implementation evidence belongs under `.nxs/evidence/NXS-P16/` only after commands execute. Before merge, rollback is abandoning/reverting the feature branch; every future migration must prove clean upgrade and repository-standard downgrade/roll-forward behavior.
- READY: only after the governance PR is merged to exact-green `main`, P16 starts through the canonical lifecycle, all mandatory behavior and adversarial tests pass locally and on exact-head GitHub CI/Security, `NXS-CAMP-001` is VALIDATED, P16 is READY/GO, the next phase is calculated without activation, and no unauthorized merge/deployment occurs.

## NXS-P17 contract

- Phase: NXS-P17; implementation branch: `feat/nxs-p17-human-agents`; registry dependencies are NXS-P09 and NXS-P13, which transitively preserve the certified P03/P04/P06 and P11/P12 boundaries without depending on P18 or later phases.
- Objective: deliver durable, tenant-isolated human-agent operations for bounded queues, explicit presence/capacity, exclusive assignment and conversation ownership, controlled AI-to-human handoff and human-to-AI return, transfers, supervisor controls and advisory copilot context.
- Requirement: `NXS-HUMAN-001`, depending on `NXS-AUTH-006`, `NXS-EVENT-003`, `NXS-CUSTOMER-001`, `NXS-WA-001`, `NXS-EMAIL-001`, `NXS-SMS-001`, `NXS-VOICE-001` and `NXS-AGENT-001`. `NXS-VOICE-001` carries the certified telephony dependency; P13 is the AI boundary. P14 is intentionally not required because P17 does not own generic workflow execution.
- Authority: PostgreSQL owns queue membership, presence, capacity, assignments, claim tokens, lease versions, conversation ownership, handoff state and supervisor mutations. WebSockets, browsers, Valkey and process-local locks may signal or optimize but never grant authority.
- Scope: work items and tenant queues; deterministic priority/eligibility ordering; explicit agent presence; bounded capacity; row-locked claim and compare-and-set fencing; AI/human ownership transfer; same-tenant agent/queue transfers; audited supervisor release/requeue/transfer; P04 events; P09-only messaging actions; P11/P12 call/session references; P13-only copilot and AI return; bounded APIs and RBAC. The detailed contract and matrices are in `docs/engineering/nxs-p17-human-agent-operations-design.md`.
- Non-scope: telephony transport/media, SIP routing, provider SDKs, message providers, LLM execution, generic workflows, campaigns/scheduling, predictive dialing, workforce forecasting, shifts/payroll, QA scoring, recording/analytics, frontend/WebRTC, external ticket synchronization, scaling, billing, global observability, automated orphan recovery/failover and deployment.
- Security/concurrency: forced RLS and tenant-aware composite FKs; trusted tenant context; `human:read`, `human:work`, `human:configure` and `human:supervise` least privilege; deterministic ordering; `FOR UPDATE SKIP LOCKED`, uniqueness, capacity serialization, opaque claim tokens and monotonically increasing lease versions. No stale token may send, transfer, complete or return control.
- Recovery boundary: P17 persists sufficient ownership and ambiguity evidence and supports explicit authorized release/transfer/requeue. P25 owns automatic stale-owner detection, lease reaping, orphan reassignment, ambiguous downstream reconciliation and failover.
- Governance status: this branch refines metadata and documentation only. It does not run `nxs-start`, create a P17 manifest, add migrations/APIs/runtime code, activate P18, merge or deploy.
- READY: governance review does not make P17 READY. Implementation may begin only after this governance change merges to exact-green `main`, a human authorizes P17, and the canonical lifecycle starts it on the required implementation branch.
