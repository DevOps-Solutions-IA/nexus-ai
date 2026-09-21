# NXS-P20 — NXS Sentinel design and certification contract

## 1. Status and objective

Governance status: PLANNED / PENDING. Implementation has not started.

P20 builds NXS Sentinel as a constrained platform SRE control plane on top of certified P04 event semantics, P08 Tool Engine, P13 Agent Runtime and P18 placement. Sentinel observes trusted operational facts, correlates incidents, produces evidence-bound diagnosis and proposes or executes only runbook-bound operations that pass durable policy, approval and fencing.

Requirement: `NXS-SRE-001`.

Canonical authority decision: ADR-0101.

## 2. Permanent authority model

```text
Trusted operational sources
        │
        ▼
bounded Sentinel adapters
        │
        ▼
PostgreSQL Sentinel state
(signal receipts / incidents / findings / proposals / approvals / executions)
        │
        ├──────────────► P13 Agent Runtime
        │                  reasoning only
        │                  structured findings/proposals
        │
        ▼
Sentinel Policy + Runbook Registry
        │
        ▼
durable approval + execution fence
        │
        ▼
P08 Tool Engine / certified read-only adapter
        │
        ▼
existing subsystem authority
```

Sentinel never replaces P04, P08, P13, P18, P11, P19, Git/NXS state or any provider's own state.

## 3. Domain model

### 3.1 SentinelSignalReceipt

Minimum durable fields:

- receipt_id UUIDv7
- adapter_id + adapter_revision
- source_kind
- source_identity
- source_observation_id
- observed_at
- received_at (DB time)
- subject_kind
- subject_id
- optional organization_id as subject metadata
- severity
- fingerprint
- bounded typed facts JSON
- sanitized evidence references
- schema_version
- processing status

Uniqueness: adapter/source observation identity. Duplicate deliveries cannot create duplicate incident effects.

### 3.2 SentinelIncident

- incident_id
- correlation_key
- subject_kind/id
- optional organization_id
- status: OPEN → TRIAGED → MITIGATION_PROPOSED → MITIGATING → MONITORING → RESOLVED → CLOSED
- severity
- first_seen_at / last_seen_at
- current_revision
- current_summary
- resolution reason/source
- timestamps

A RESOLVED transition requires a trusted recovery observation or authorized operator decision; time passage alone is insufficient.

### 3.3 SentinelFinding

- finding_id
- incident_id
- finding_revision
- hypothesis category
- explanation
- confidence
- evidence references
- model/provider identity and request fingerprint
- created_at

Finding text is advisory. It cannot itself satisfy action policy.

### 3.4 SentinelRunbookDefinition

- runbook_id
- immutable revision
- stable key
- handler/tool key
- risk_class: OBSERVE | DIAGNOSTIC | REVERSIBLE | HIGH_IMPACT | DESTRUCTIVE
- allowed target kinds
- typed parameter schema digest
- required permission/policy
- timeout and bounded retry policy
- idempotency semantics
- expected preconditions/postconditions
- compensation metadata
- enabled status

Only source-registered handlers may execute. No executable source, shell, SQL or arbitrary endpoint is stored in runbooks.

### 3.5 SentinelActionProposal

- proposal_id
- incident_id
- proposal_revision
- runbook_id/revision
- target kind/id
- optional organization_id
- target authority generation/revision when applicable
- canonical parameters
- semantic fingerprint
- risk class
- policy revision
- state: PROPOSED | APPROVED | REJECTED | EXPIRED | EXECUTING | SUCCEEDED | FAILED | AMBIGUOUS | CANCELLED
- created_by type (MODEL | OPERATOR | SYSTEM)
- expiry

### 3.6 SentinelApproval

- approval_id
- proposal_id
- proposal fingerprint
- approver principal
- decision
- reason code
- issued_at/expires_at
- policy revision

Approval is unusable after proposal, runbook, target generation or policy changes.

### 3.7 SentinelExecution

- execution_id
- proposal_id unique active slot
- execution_generation
- owner_id
- lease_expires_at DB time
- idempotency_key
- dispatch_state: CLAIMED | DISPATCHED | COMPLETED | AMBIGUOUS
- started_at/completed_at
- sanitized result/error classification
- external receipt/reference when safe

## 4. Scope classification

Sentinel is an internal platform capability. Tenant users do not obtain Sentinel control APIs in P20.

Signals may describe GLOBAL, SERVICE, CELL or ORGANIZATION subjects. Organization-scoped facts must originate from trusted adapters and preserve existing RLS when tenant-owned source data is queried. Sentinel global tables are platform-control data and must not become a bypass path for tenant business data.

## 5. Signal adapters

Initial certification must include at least:

1. NXS phase/control state adapter (read-only).
2. PostgreSQL/service dependency health adapter with bounded safe queries.
3. NATS/JetStream health adapter using existing client boundaries.
4. Cell placement/inventory health adapter using P18 service APIs/repositories, not duplicate authority.
5. SIP edge/telephony health adapter using safe P11/P19 status surfaces.
6. Generic HTTP health adapter restricted to operations-owned statically allowlisted service endpoints, if implemented.

No adapter accepts caller-provided arbitrary host, SQL or credential.

## 6. Reasoning contract

P13 may receive only a bounded incident context object:

- sanitized signal facts
- safe evidence references/snippets
- known topology identifiers
- relevant runbook catalog metadata
- previous findings/proposals
- explicit policy constraints

The model returns a strict schema: findings and optional proposed runbook key/revision plus typed parameters. Free-form tool names, URLs, shell commands, SQL, credentials or target overrides are rejected.

Prompt injection in logs, tenant content or provider errors is treated as data, never instruction authority.

## 7. Policy and risk

Default hardened behavior:

- OBSERVE: autonomous allowed.
- DIAGNOSTIC: autonomous allowed if non-mutating and within budgets.
- REVERSIBLE: durable human approval required.
- HIGH_IMPACT: denied in P20.
- DESTRUCTIVE: denied in P20.

A future phase may certify broader automation, but P20 evidence cannot claim it.

A global execution kill switch blocks new mutable executions while allowing observation and incident recording.

## 8. Proposal fingerprint

Canonical SHA-256 fingerprint binds:

- incident_id + incident_revision
- runbook_id + immutable revision
- target kind/id
- relevant target generation/revision
- canonical typed parameters
- risk class
- policy revision
- requested operation identity

Any mismatch invalidates approval and execution.

## 9. Locking and concurrency

Canonical lock order for Sentinel-owned rows:

1. incident
2. proposal
3. approval/current policy snapshot
4. execution slot/lease
5. existing subsystem admission/authority through its own public/internal service boundary

Never hold a Sentinel transaction across external provider/tool network I/O.

Concurrent duplicate signals converge by unique receipt/correlation constraints.

Concurrent proposals with the same semantic fingerprint converge deterministically.

Only one execution owner may cross dispatch for a proposal. Lease recovery requires expiry by DB time plus a compare-and-swap generation increase. The stale owner must fail at the final pre-dispatch fence.

## 10. Side-effect boundary

Before dispatch, in one local transaction Sentinel revalidates:

- proposal state/fingerprint
- current runbook revision/enabled state
- current policy revision
- required approval and expiry
- target identity and bound generation/revision
- execution ownership/generation/lease
- kill switch
- risk class

After commit, the executor calls only the registered P08 tool/adapter using the stable idempotency identity.

If response is definitely rejected before any side effect, mark FAILED.

If side effect may have occurred and response is lost, mark AMBIGUOUS and do not auto-retry with a new identity.

## 11. Security

Required:

- dedicated platform service identity
- least-privilege database grants
- no BYPASSRLS runtime shortcut
- no arbitrary shell/SQL/SSH
- no arbitrary outbound URL/host
- no secrets in prompts, events, evidence or logs
- strict JSON/schema bounds
- bounded evidence snippets
- safe error taxonomy
- model output treated as untrusted input
- runbook handlers source-registered
- approval principal authenticated through existing auth/RBAC
- CSRF/session/API protections inherited from platform APIs where exposed
- constant-time comparison for opaque approval/execution tokens if such tokens exist
- no tenant ability to create executable runbooks or grant platform permissions

## 12. Resource bounds

Configuration must define finite limits for:

- signals per poll/batch
- signal body/facts size
- evidence references per signal
- open incidents scanned per cycle
- findings per incident
- proposals per incident
- active executions
- execution timeout
- adapter timeout
- model timeout
- retries for proven-safe read-only operations
- retention/cleanup batch sizes

No unbounded table scan or fan-out is acceptable.

## 13. Events and history

Sentinel state changes may emit P04 events with safe identifiers/reasons. Business correctness remains PostgreSQL-authoritative.

P20 history is sufficient for replay/idempotency and engineering evidence but does not claim P22 enterprise audit retention/non-repudiation.

## 14. APIs

Any internal/operator API must be typed, permissioned and deny-by-default.

Candidate permissions:

- `sentinel:read`
- `sentinel:triage`
- `sentinel:approve`
- `sentinel:control`

No tenant role receives them by default.

The API never accepts raw handler names outside registered identifiers, shell, SQL, arbitrary URLs, credentials or direct provider payloads.

## 15. Migrations

Additive Alembic migrations only. Tenant-aware FKs when organization_id references tenant entities. Platform-control tables must have explicit database grants and must not be accidentally exposed to `nexus_runtime` tenant endpoints.

One Alembic head. Upgrade from current canonical P19 main, fresh upgrade, disposable downgrade/re-upgrade and schema guard all required.

## 16. Failure and recovery semantics

- PostgreSQL unavailable: no proposal/approval/execution authority; fail closed.
- P13 unavailable: incident remains; no fabricated diagnosis or action.
- P08 unavailable: execution remains durable; no alternate direct provider path.
- adapter timeout: safe failure receipt; no incident resolution by absence.
- stale approval: deny.
- target generation changed: deny.
- lease expires before dispatch: stale owner denied.
- lost response after dispatch: AMBIGUOUS; no automatic re-dispatch.
- process crash before dispatch: lease recovery may continue only after fenced ownership transfer.
- process crash after dispatch: treat as potentially ambiguous unless a stable external idempotency/result lookup proves outcome.
- kill switch active: mutable execution denied.

## 17. Concurrency/failure matrix C01–C24

- C01 duplicate signal same source observation.
- C02 concurrent duplicate signal insert.
- C03 two signals correlate same incident.
- C04 same fingerprint across different subjects must not collide.
- C05 concurrent finding generation cannot overwrite revisions.
- C06 concurrent equivalent proposal converges.
- C07 proposal changes invalidate old approval.
- C08 runbook revision changes invalidate old approval.
- C09 policy revision changes invalidate old approval.
- C10 target generation changes before execution.
- C11 two executors claim same proposal.
- C12 lease expires while stale owner still running.
- C13 recovered owner fences stale owner pre-dispatch.
- C14 crash after claim before dispatch.
- C15 crash after dispatch before response.
- C16 external timeout with possible side effect.
- C17 proven pre-dispatch rejection.
- C18 P08 unavailable.
- C19 P13 unavailable.
- C20 PostgreSQL unavailable.
- C21 kill switch races execution.
- C22 approval expiry races dispatch.
- C23 incident resolution races new signal.
- C24 bounded cleanup races live execution.

Each case requires deterministic barriers/transactions where concurrency matters; sleeps alone are not proof.

## 18. Acceptance criteria AC01–AC36

- AC01 P20 manifest/requirement/schema lifecycle valid.
- AC02 dedicated Sentinel package/domain boundaries.
- AC03 additive migration and one Alembic head.
- AC04 least-privilege DB grants and tenant isolation.
- AC05 trusted adapter registry; arbitrary adapters denied.
- AC06 source observation idempotency.
- AC07 deterministic incident correlation.
- AC08 explicit incident state machine.
- AC09 evidence-bound findings with model metadata.
- AC10 strict P13 structured-output contract.
- AC11 prompt/log injection cannot create authority.
- AC12 immutable runbook revisions.
- AC13 no executable shell/SQL/arbitrary URL in runbook data.
- AC14 source-registered handler allowlist.
- AC15 risk classification enforced server-side.
- AC16 default hardened policy allows autonomous OBSERVE/DIAGNOSTIC only.
- AC17 REVERSIBLE action requires durable approval.
- AC18 HIGH_IMPACT/DESTRUCTIVE denied.
- AC19 proposal semantic fingerprint canonical/deterministic.
- AC20 approval bound to exact fingerprint and expiry.
- AC21 changed target generation fails closed.
- AC22 execution CAS/lease fencing.
- AC23 stale owner cannot dispatch.
- AC24 P08 is the only mutable action path.
- AC25 stable idempotency identity for dispatch.
- AC26 ambiguous effect stops automatic retry.
- AC27 global kill switch fences mutable execution.
- AC28 finite resource budgets enforced.
- AC29 no secret leakage in logs/events/prompts/evidence.
- AC30 safe P04 event integration where emitted.
- AC31 P04/P08/P13/P18 regression suites pass.
- AC32 real PostgreSQL integration/concurrency tests.
- AC33 API/RBAC negative tests for operator permissions.
- AC34 clean-room from current canonical main passes.
- AC35 full suite coverage remains >= repository threshold.
- AC36 exact implementation-head NXS CI and Security succeed.

## 19. Required evidence

Under `.nxs/evidence/NXS-P20/` produce machine-readable evidence for:

- governance/preflight
- migrations/schema/grants
- signal adapters/idempotency
- incident correlation
- agent structured reasoning
- runbook registry
- policy/risk
- approvals
- execution fencing/concurrency
- ambiguous effects
- kill switch
- security/RBAC/secrets
- resource bounds
- events/history
- regression
- C01-C24 map
- AC01-AC36 map
- tests/coverage
- clean-room
- Docker/container security
- git/exact-head CI

No PASS may be recorded for an unexecuted action.

## 20. Definition of READY

P20 may close READY/GO only when every mandatory gate passes, C01-C24 and AC01-AC36 are fully mapped/executed, exact implementation-head CI/Security are green, external implementation audit authorizes closure, canonical closure succeeds and closure-head checks pass.

READY does not mean production deployed, self-healing production, full observability, compliance, capacity, failover, DR or chaos certified.
