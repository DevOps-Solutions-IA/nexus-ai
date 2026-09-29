# NXS-P22 — Audit Platform

## Admission and authority

This admission design incorporates the authorized scope decision P22-SD01. It is not
a certification receipt. Canonical main
at admission is `d23f124f0a1a94d0cefe37e0fc8e5f9f17ea2b6e`. P21 PR #48 merged at
`53149934906d0d243cb3f593efc8a5bbf74bd793`; P21 is READY/GO on main. P22 is initially
PLANNED/PENDING on `feat/nxs-p22-audit`. The canonical start, not this document,
establishes BUILDING/PENDING. No deployment or P23 start is authorized.

NXS-AUDIT-002 remains the existing mandatory requirement. P04 and P21 remain the
registered dependencies. P22 records evidence about actions; P03 authorizes, P06 owns
customer semantics, P08 tools, P13 agent execution, P16 send eligibility, P20 platform
SRE, and P21 compliance decisions. An audit record grants no business authority.

## Integrity architecture

Use a PostgreSQL tenant-scoped SHA-256 chain rather than a global chain or independent
row digests. Independent digests cannot detect interior deletion; a global chain would
serialize unrelated Organizations and risk exposing cross-tenant predecessors.

`audit_records` stores immutable facts, unique source identities and per-tenant sequence
numbers. `audit_heads` stores one Organization's last sequence and digest. Both use
forced RLS and Organization foreign keys. A transaction inserts the head if absent,
locks it, checks replay identity, assigns the next sequence and predecessor, inserts the
record and advances the head. No application-local lock supplies authority. Different
Organizations lock different rows. A duplicate returns the original immutable fact;
the same source identity with changed semantics raises a terminal conflict.

Canonical UTF-8 JSON uses sorted keys, compact separators, finite typed values and UTC
timestamps. SHA-256 binds the complete versioned fact, tenant, sequence, predecessor,
record identity and persistence time. A distinct semantic digest binds source content
before persistence metadata is assigned. No client supplies either digest or sequence.
Runtime has SELECT/INSERT but no UPDATE/DELETE on committed records; a database trigger
also rejects mutation. Heads are mutable bookkeeping, not an authorization API.

Verification takes a consistent tenant snapshot, scans a bounded page in sequence order
and checks content digest, predecessor, expected sequence and tenant identity. Results
identify the verified range and anchor; a bounded partial verification is not labeled
whole-history verification. Snapshot high-water marks keep concurrent append outside
the chosen range. Missing interior rows, modifications and cross-chain substitution
fail verification. An externally retained anchor is not introduced: a hostile database
owner who rewrites all facts and anchors is outside the claimed threat model.

Corrections, if emitted, are new facts referencing a same-tenant original; the original
is never modified. Audit history has no destructive retention API. P21 legal-hold and
retention decisions are not reimplemented. Future retention needs an explicit contract
that preserves integrity anchors and hold semantics.

## Source intent and P04 ingestion

Registered source code constructs typed audit intent in the source transaction using
the existing P04 transactional outbox. No public ingestion endpoint exists. The source
mutation and its intent commit atomically where the source is a local DB mutation.
External provider effects cannot be made atomic with PostgreSQL; their recorded local
outcome/ambiguity and intent can be atomic, without claiming certainty about the remote
effect. A consumer writes ledger facts asynchronously through P04 DurableConsumer.
Intent-to-ledger delivery is durable eventual processing, not the original transaction.

Use an explicit audit-intent payload version and finite producer/action registry, not
arbitrary event-to-log conversion. The consumer verifies envelope scope, source, event
type, payload and committed outbox provenance before acknowledging even a replay. A
finished generic P04 receipt must not bypass same-identity semantic-conflict detection.
The ledger effect and durable receipt commit before ACK. Lost ACK or a crash after
commit redelivers the same fact. Failures before commit leave no fact or successful
receipt. Bounded P04 retry and terminal dead-letter handling remain transport authority.

Ledger append emits no new equivalent audit intent. Unknown producer/action/version,
cross-tenant envelope, changed replay content, missing actor and unsafe metadata fail
closed. Failed source transactions create no audit success. Database outage never
becomes a successful acknowledgment. Terminal poison handling uses sanitized reason
codes, not payload/exception logging or a misleading successful audit fact.

## Actor provenance

Human identity comes from P03 verified token plus live membership/session validation,
not body fields or the crypto-only Principal dependency. Request-scoped provenance is
bound only after that validation and checked against the source transaction's tenant.
Internal Principal-taking services explicitly bind the validated actor. Missing human
provenance is an error, never silently relabeled SYSTEM.

AI execution derives agent/session/turn identities from P13-owned persisted state.
Initiating human identity is separate from the executing AI actor. The Principal used
by P13 to authorize P08 is not evidence that a human executed the AI tool call. Worker
entrypoints explicitly override inherited request context. System/service identity is
an allow-listed source identity, not a synthetic human or Organization.

Previously occurred facts are not invalidated by later membership revocation. Queries
use current P03 authorization; asynchronous ingestion uses the committed source intent,
not an assertion that the original actor is still permitted to perform the operation.
No retrospective actor attribution is fabricated for old events lacking that evidence.

## Record, metadata and time contract

Facts bind record UUID, Organization, producer, trusted source operation/event UUID,
occurred_at, DB recorded_at, actor type/identity/context, action, target type/UUID,
outcome, correlation/causation/request identity, schema version, typed metadata and
integrity version/digest/predecessor/sequence. HUMAN, AI_AGENT and SYSTEM/SERVICE are
distinct. Timestamps must be timezone-aware UTC; chain order is not wall-clock order.

Metadata contains only declared IDs, finite enums, bounded counts and safe reason codes.
No generic nested blob or raw request is accepted. Explicit limits cover bytes, fields,
strings, query windows/pages, verification pages, consumer batches, concurrency and
timeouts. Credentials, authorization headers, tokens, private keys, full prompts/model
context, raw webhooks and identity documents are rejected, never captured for debugging.
Prompt-like strings are untrusted data and cannot select actor, producer or handler.

Business events describe domain changes. Audit records preserve attributable facts.
Security logs diagnose security behavior. Application logs diagnose software. Compliance
evidence supports P21 decisions. None substitutes for the others or grants authority.

## Query boundary

Versioned `/api/v1/audit` read-only endpoints require trusted tenant resolution, live
P03 principal and explicit `audit:read` permission; verification requires an explicit
permission. Organization owners receive the narrowly defined capabilities via migration;
ordinary members receive none. Repositories remain RLS scoped. Unknown/foreign record
IDs return the same not-found contract. There is no UPDATE/DELETE endpoint.

Stable cursor pagination binds an immutable sequence and snapshot high-water mark,
never an offset. A finite maximum page size and bounded time/filter inputs apply.
Only declared actor/action/resource/outcome/correlation/source filters exist; strings
are bound SQL parameters, never expressions, table selectors or executable selectors.
Strict Pydantic schemas and canonical RFC 9457 errors reject invalid inputs safely.

## Producer inventory and integration obligations

Status below is a design commitment, not executed proof. SUPPORTED requires a source
hook plus tests before certification; missing mandatory coverage blocks Stage A.

| Subsystem / action family | Owner; actor; tenancy | Existing event/correlation | P22 producer status; metadata; limitations |
| --- | --- | --- | --- |
| Organization profile/lifecycle | P02/P05; human or registered provisioning service; Organization | Service transaction, request context; not all paths emit events | SUPPORTED; target ID/version/state only; preserve owning service semantics |
| Membership/role administration | P03; authenticated human or explicit bootstrap service; Organization | MembershipService transactions; no complete outbox producer | SUPPORTED; membership/assignment IDs and enum change; no tokens/passwords |
| Tenant session lifecycle | P03; authenticated user; selected Organization | AuthService local transactions and security logs | SUPPORTED for tenant-bound successful/session state facts; global pre-tenant login failures remain security logs, NOT_APPLICABLE to tenant facts |
| Customer/identity/conversation mutations | P06; human or registered channel service; Organization | Typed events, source transaction, correlation | SUPPORTED; IDs and bounded lifecycle facts, no identity values or content |
| Tool execution | P08; human or P13 AI; Organization | Execution receipt has caller; completion event currently separate transaction | SUPPORTED; add intent to receipt transaction; external effect remains separately bounded, no arguments/results/secrets |
| Agent execution | P13; AI plus distinct initiating human; Organization | Typed session/turn/tool events; persisted agent/session/turn references | SUPPORTED; IDs/outcome only, no prompts, context or chain of thought |
| Workflow administration | P14; human or registered service; Organization | Version/run events and correlation; actor capture required | SUPPORTED; definition/version/run IDs and state, no workflow inputs/outputs |
| Campaign administration | P16; human or registered scheduler; Organization | Transactional events/transition history | SUPPORTED; IDs/state only; no new send/consent authority |
| Human-agent operations | P17; human or explicit AI handoff; Organization | Typed events carry source actor_user_id and durable history | SUPPORTED; assignment/queue/work IDs and bounded state; no message bodies |
| Compliance policy/holds/subject requests | P21; live authenticated human; Organization | Transactional compliance.state.changed, actor available in service transaction | SUPPORTED; resource IDs/state; no subject exports or verification evidence content |
| Sentinel tenant-owned business actions | P20; no such authority | Prohibited by ADR-0101 | NOT_APPLICABLE; Sentinel is not a tenant business actor |
| Sentinel global observation/diagnostic facts | P20; platform service; no Organization owner | Separate nexus_sentinel state | DEFERRED by P22-SD01 wherever audit capture requires a global ledger |
| Sentinel global privileged approvals/executions | P20; platform operator/service; no Organization owner | Separate nexus_sentinel transactions and platform grants | DEFERRED by P22-SD01; no tenant attribution |
| Sentinel kill-switch/global risk-policy mutation | P20; platform operator; no Organization owner | Platform controls, no tenant transactional outbox | DEFERRED by P22-SD01; known privileged audit gap, not NOT_APPLICABLE |

## Platform-global audit boundary

P22's certified runtime scope is tenant-owned audit records. Platform-global Sentinel
privileged operations require a different authority boundary because they have no
legitimate Organization owner. They are explicitly DEFERRED by Master Orchestrator
decision P22-SD01. The former admission blocker is resolved, not a runtime exclusion
invented by the implementation agent. No universal audit claim is permitted.

The only permitted coverage claim is: "Tenant-scoped immutable/tamper-evident audit
coverage for the supported action and producer families documented in the P22 producer
inventory." Audit Platform is a phase name, not a claim of complete enterprise or
platform-global audit coverage. Global facts must fail closed at tenant ingestion.

Source inspection at the exact baseline established:

- `sentinel/control.py:SentinelOperatorAuthority.require` checks a platform grant,
  not an Organization audit capability. `SentinelControl.set_mutable_actions`,
  `transition` and `execute` are privileged platform actions.
- `sentinel/database.py:SentinelDatabase.transaction` rejects a nonempty tenant GUC;
  `nexus_sentinel` has no tenant outbox grants. ADR-0101 explicitly says Sentinel is
  not a tenant and diagnostic Organization references are not tenant authority.
- P04's tenant outbox needs Organization ownership. Its direct global publisher has
  no same-transaction tenant intent and cannot be presented as such.

The execution contract requires a tenant-scoped ledger. P22-SD01 explicitly authorizes
deferring Sentinel's platform-global coverage. Choosing
the operator's login Organization as owner of a platform-wide kill-switch change would
misstate provenance and potentially expose platform facts to tenant readers. A fake
system Organization is not a solution. A separate platform ledger/query authority is
an explicit scope extension, not an inference from tenant audit permission.

P22-SD01 selects tenant-only P22 with explicit platform-global deferral, not a separate
platform audit lane. Global unauthenticated login failures likewise
cannot be assigned a trusted tenant from request data; existing security logs remain
distinct from tenant audit records. No retrospective facts are fabricated.

### Deferred design: Privileged platform-global audit authority

This durable gap remains visible for P27 security-hardening and P30 backend-certification
planning; it is not a P22 Stage A implementation obligation. A future scoped contract
must cover Sentinel privileged approvals and executions, kill-switch and global
risk-policy mutation, authenticated global actor/operator provenance, durable global
ingestion, append-only platform ledger authority, independent access-control rules,
global integrity/tamper-evidence semantics and retention authority. It must prohibit
fictitious Organizations and cross-tenant exposure. P04 GLOBAL EventEnvelope is a
possible future primitive only: publish_global is direct confirmed publication, not
the tenant transactional outbox and not durable authoritative Sentinel audit capture.
No partial global Sentinel integration is implemented or certified by P22.

## Concurrency and failure certification

Real PostgreSQL deterministic barriers/transactions, not sleep-only simulations:
C01 same-tenant appends; C02 different tenants; C03 redelivery; C04 concurrent duplicate;
C05 semantic replay conflict; C06 post-insert/pre-ACK crash; C07 lost ACK; C08 chain
assignment; C09 stale predecessor; C10 verification during append; C11 chain isolation;
C12 foreign record ID; C13 pagination during append; C14 correction/original race;
C15 source rollback/intent; C16 committed outbox/consumer; C17 revoked membership and
historical fact; C18 same actor across tenants; C19 poison retry/terminal; C20 recursion.

Security covers forged tenant/actor/source/event, foreign IDs and CRUD, runtime mutation,
digest/predecessor substitution, changed replay, SQL/shell/URL-shaped selectors/metadata,
oversized/deep metadata, secrets/tokens/headers, prompt injection, future versions,
unknown actions/producers, inactive membership, role escalation and unbounded queries.
Failure evidence preserves unsuccessful attempts. No mocked database authority, weakened
coverage or silent skips qualify for certification.

## Acceptance and execution plan

AC01 existing mandatory requirement; AC02 valid manifest; AC03 dependency equality;
AC04 persistence; AC05 forced RLS; AC06 trusted tenant; AC07 trusted producer; AC08 trusted
actor; AC09 action allowlist; AC10 bounded metadata; AC11 secret exclusion; AC12 no UPDATE;
AC13 no DELETE; AC14 append corrections; AC15 deterministic identity; AC16 duplicate
convergence; AC17 changed replay rejection; AC18 deterministic integrity; AC19 no fork;
AC20 tamper detection; AC21 tenant chain isolation; AC22 P04 reuse; AC23 accurate atomicity;
AC24 lost ACK; AC25 no recursion; AC26 human/AI distinction; AC27 correlation/causation;
AC28 explicit time; AC29 authenticated RBAC queries; AC30 bounded stable pagination;
AC31 no SQL filter language; AC32 isolation attacks; AC33 P21 authority preserved;
AC34 P04 authority preserved; AC35 migration/schema; AC36 regression/coverage; AC37 exact
CI; AC38 exact Security; AC39 accurate canonical/branch docs; AC40 no certification claims;
AC41 explicit Sentinel global deferrals/no universal claim; AC42 no global Sentinel
attribution to a login/arbitrary Organization; AC43 no misrepresentation of direct P04
GLOBAL publication as transactional durable audit capture.

Implementation proceeds admission, canonical start, typed contracts/integrity with unit
tests, tenant persistence/migration with real DB tests, P04 ingestion/provenance/replay,
source producers/actor tests, authenticated query/verification tests, concurrency and
adversarial matrices, then complete local certification and exact-head candidate CI.
Each implemented surface must have exact executable test references in the evidence map.
Additive migration must prove fresh/P21 upgrade, one head, check/schema guard, disposable
downgrade and re-upgrade including immutable triggers/permissions. Full combined canonical
coverage remains at least 90%; no P22 exclusion. Docker, multiarch, non-root, health,
shutdown, clean-room, SAST, secrets, dependency and container scans are mandatory.
HIGH/CRITICAL block; MEDIUM requires review. No historical suite count certifies new code.

## Non-scope and rollout

No P23 metering/billing, P24 observability, P25 recovery/failover, P26 backup/DR, P27 final
hardening, P28 capacity, P29 chaos, P30 backend certification, P31 release, P32 deployment,
frontend, SIEM, logging/tracing/metrics replacement, arbitrary log/event/blob ingestion,
arbitrary SQL/table selection, raw prompts/credentials/tokens, model audit authority,
cross-tenant audit administration or retrospective fabrication. No legal, GDPR, HIPAA,
SOC 2, ISO, WORM, infinite retention or hostile-database-owner tamper-proof claim.
P22 Stage A remains BUILDING/PENDING with implementation/closure fields null. Only
external Master Orchestrator audit can authorize Stage B. No merge or deployment.
