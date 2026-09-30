# NXS-P22 — Audit Platform

## Admission and authority

This design incorporates the authorized dual-scope decision that SUPERSEDES P22-SD01.
Historical admission evidence remains preserved as historical, not current certification.
Canonical main
at admission is `d23f124f0a1a94d0cefe37e0fc8e5f9f17ea2b6e`. P21 PR #48 merged at
`53149934906d0d243cb3f593efc8a5bbf74bd793`; P21 is READY/GO on main. P22 admission
was PLANNED/PENDING on `feat/nxs-p22-audit`. Canonical start executed once at
2026-09-29T20:45:39Z and established BUILDING/PENDING, NXS-AUDIT-002 IN_PROGRESS.
No deployment or P23 start is authorized.

NXS-AUDIT-002 remains the existing mandatory requirement. P04, P20 and P21 are the
registered dependencies. P22 records evidence about actions; P03 authorizes, P06 owns
customer semantics, P08 tools, P13 agent execution, P16 send eligibility, P20 platform
SRE, and P21 compliance decisions. An audit record grants no business authority.

The requirement retains NXS-TENANT-001 and explicitly depends on already VALIDATED
NXS-EVENT-003 for durable tenant outbox authority and NXS-SRE-001 for the newly authorized
Sentinel platform producer integration. This refines real consumed authority without
duplicating requirements. Corrective 01 adds P20 to the phase dependency mapping:
the lifecycle guard requires completed direct phase authorities, and the requirement's
NXS-SRE-001 dependency explicitly binds the Sentinel authority consumed here. All three
phase dependencies are READY/GO and all requirement dependencies are VALIDATED.

## Integrity architecture

Use PostgreSQL tenant-scoped SHA-256 chains and independently serialized PLATFORM domains, not one global
chain shared by tenants or independent
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

Facts bind explicit scope, record UUID, Organization (required for TENANT, null for
PLATFORM), producer, trusted source operation/event UUID,
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
| Tool execution | P08; human or P13 AI; Organization | Durable dispatch-authorized intent before I/O; outcome intent shares receipt transaction | SUPPORTED; operation identity links attempt/result; dispatch authorization never claims remote success, no arguments/results/secrets |
| Agent execution | P13; AI plus distinct initiating human; Organization | Typed session/turn/tool events; persisted agent/session/turn references | SUPPORTED; IDs/outcome only, no prompts, context or chain of thought |
| Model account credential rotation | P13/P07; authenticated human or registered service; Organization | Local encrypted vault and account reference | SUPPORTED for TransactionalVaultClient/local PostgreSQL vault only; encrypted material, pointer and audit intent share a transaction; no credential contents in audit |
| External nontransactional credential vault mutation | P13/P07; tenant action | External effect cannot join local transaction | DEFERRED/unsupported mutation fails closed until a durable audited adapter exists; no silent fallback |
| Workflow administration | P14; human or registered service; Organization | Version/run events and correlation; actor capture required | SUPPORTED; definition/version/run IDs and state, no workflow inputs/outputs |
| Campaign administration | P16; human or registered scheduler; Organization | Transactional events/transition history | SUPPORTED; IDs/state only; no new send/consent authority |
| Human-agent operations | P17; human or explicit AI handoff; Organization | Typed events carry source actor_user_id and durable history | SUPPORTED; assignment/queue/work IDs and bounded state; no message bodies |
| Compliance policy/holds/subject requests | P21; live authenticated human; Organization | Transactional compliance.state.changed, actor available in service transaction | SUPPORTED; resource IDs/state; no subject exports or verification evidence content |
| Sentinel tenant-owned business actions | P20; no such authority | Prohibited by ADR-0101 | NOT_APPLICABLE; Sentinel is not a tenant business actor |
| Sentinel global observation/diagnostic facts | P20; platform service; no Organization owner | Separate nexus_sentinel state | DEFERRED for general raw diagnostic ingestion; registered execution outcomes are included below, not arbitrary logs |
| Sentinel global privileged approvals/executions | P20; platform operator/service; no Organization owner | Separate nexus_sentinel transactions and platform grants | SUPPORTED implementation obligation in PLATFORM; typed IDs/state, distinct operator/service; external effects are not database-atomic |
| Sentinel kill-switch/global risk-policy mutation | P20; platform operator; no Organization owner | Platform control transactions | SUPPORTED implementation obligation in PLATFORM through durable same-transaction intent; no tenant attribution |
| General platform grant administration | P03/P05; global identity plane | Test/future-admin insertion seam; no certified authenticated administration workflow | DEFERRED; manual DBA, migrations and test fixtures are not application audit guarantees; nullable grantor metadata is not authenticated actor provenance |
| Pre-tenant authentication failures | P03; global identity plane | Security logs, no registered durable audit producer | DEFERRED; no manufactured tenant owner or universal claim |

## Platform-global audit boundary

P22 implements explicit TENANT and PLATFORM audit authority. The dual-scope decision
SUPERSEDES P22-SD01's global deferral. Platform-global Sentinel actions have no legitimate
Organization owner: PLATFORM organization_id is always null, including when an operator
authenticated using a tenant session: the operator's login Organization is not an audit
owner. Global facts fail closed at tenant ingestion;
tenant facts cannot enter the platform ledger. No fake system Organization may be created.
Coverage claims must name the supported producer/action families in this inventory;
there is no unproven universal coverage claim.

Source inspection at the exact baseline established:

- `sentinel/control.py:SentinelOperatorAuthority.require` checks a platform grant,
  not an Organization audit capability. `SentinelControl.set_mutable_actions`,
  `transition` and `execute` are privileged platform actions.
- `sentinel/database.py:SentinelDatabase.transaction` rejects a nonempty tenant GUC;
  `nexus_sentinel` has no tenant outbox grants. ADR-0101 explicitly says Sentinel is
  not a tenant and diagnostic Organization references are not tenant authority.
- P04's tenant outbox needs Organization ownership. Its direct global publisher has
  no same-transaction tenant intent and cannot be presented as such.

The platform lane uses immutable platform source intents, platform records, independent
integrity heads and processing receipts. Each supported Sentinel local transaction appends
its typed intent before commit. nexus_sentinel receives INSERT-only source-intent
privileges, not ledger read/write authority. Its non-tenant checks and business authority
remain intact.

The dedicated nexus_audit_platform role has no superuser, BYPASSRLS, role membership,
schema CREATE or tenant access. A bounded worker reads committed intents and atomically
appends an idempotent fact and receipt. Crash before commit retries unchanged; crash after
commit returns the same fact. Changed semantics under the same identity fail closed.
Platform heads never lock tenant heads. Intent remains durable while workers or
brokers are unavailable; source mutation rolls back if intent persistence fails.
Remote effects are not database-atomic: dispatch and observed/ambiguous outcomes remain
separate facts, never fabricated completion.

Platform query/verification uses a bounded separate control interface with verified live
authentication and explicit platform grants. Tenant capabilities grant no platform access.
The dedicated connection is not placed in the tenant request lifecycle. Platform
operator/service provenance excludes synthetic tenant principals and login-org ownership.

P04 GLOBAL envelopes may represent platform facts, but publish_global is direct confirmed
publication, not durable transactional ingestion. The narrow platform source journal
supplies that missing durable boundary, not a new generic broker or replacement tenant
outbox. Neither scope implements destructive retention. Future retention must establish
explicit authority and preserve integrity/hold requirements.

### Platform worker operation

Run the independent worker with `uv run python -m nexus_ai.audit.platform`; `--once`
processes one bounded batch for controlled draining. Operations supplies
`NXS_AUDIT_PLATFORM_DSN` through its secret channel with the dedicated
`nexus_audit_platform` identity. Never put this credential in tenant settings, request
bodies, logs or audit metadata. Local bootstrap creates a development-only password;
production must supply its own secret out of band. The worker performs DB role checks,
bounded polling and graceful shutdown; failures imply no successful processing.

The internal `PlatformAuditControl` facade requires live authenticated tokens and
explicit `audit:platform:read` or `audit:platform:verify` grants. No tenant owner receives
those grants automatically. It is not a tenant-facing HTTP route or a cross-tenant query
feature. Pending source intents survive worker outages, and terminal invalid intents
remain inspectable without becoming successful audit facts. P24 metrics/alerting and
destructive journal retention are not introduced here.

### Corrective 01: platform integrity partitioning and claims

New facts use integrity version 2 and one of 16 fixed domains `platform:v2:00` through
`platform:v2:0f`. The domain is derived only in registered server code: SHA-256 of the
registered producer identity, a colon, and canonical trusted target UUID; the first
digest byte modulo 16 selects the domain. Request bodies cannot select domains, source
identities or target authority. Same producer/target operations remain ordered in one
domain, while unrelated targets distribute across independent locks. Collisions are
intentional bounded serialization, not an integrity collision. A single hot target can
still be a hot domain; no unbounded scalability or cross-domain total order is claimed.

The domain is bound into the immutable fact and SHA-256 digest. Each domain independently
assigns sequence and predecessor. The shard count and hash algorithm are versioned:
future repartitioning requires an explicit migration/version, never an in-place count
change that silently moves existing facts. Head 1 is reserved for `platform:v1:legacy`;
its records and digests are preserved, not rewritten. New heads 2–17 do not lock it.

Workers claim one pending durable source row at a time with `FOR UPDATE SKIP LOCKED`
inside a bounded batch. The claim, domain append, head update and processing receipt
commit atomically. Other workers skip the claimed source and can progress in other
domains. Batch processing also attempts its domain head with NOWAIT; a busy head rolls
back that claim and the bounded batch excludes the attempted source while considering
other pending sources. This avoids waiting behind one contended head, without promising
unbounded queue fairness beyond the configured batch budget. Direct replay processing
may wait on the requested domain with the configured database timeout.
A crash before commit rolls back and releases row ownership; after commit,
replay returns the existing logical fact. There is no durable stale lease to steal or
renew. A disconnected transaction cannot later finalize work owned by a new transaction.
Invalid intents receive terminal receipts; they do not consume an integrity sequence
or repeatedly block healthy domains. No transaction spans external I/O.

Verification/query requests select a validated domain, not an append shard. Multi-domain
verification uses explicit per-domain ranges and snapshot anchors; completeness means
all requested domains were fully scanned, not merely one page. A full platform proof
must include all 16 version-2 domains and the legacy domain. Tamper in any requested
domain fails verification. Tenant and platform heads remain disjoint.

The additive corrective migration preserves existing version-1 facts and source
identities. Downgrade must fail closed if version-2 records exist rather than discard,
renumber or rewrite immutable history. Empty version-2 domains can be removed and
legacy history retained for downgrade/re-upgrade; disposable roundtrip certification
must exercise this supported reversible state and the populated-data refusal.

### Platform grant administration inventory

There is no certified authenticated grant/revoke administration surface. This does not
mean no mutation code exists: `OrganizationProvisioner.grant_create_capability` is a
runtime-callable internal seam explicitly reserved for tests and future administration.
All shipped callers are tests. It opens the ordinary `nexus_runtime` transaction and
calls `PlatformGrantRepository.grant` for `organization:create`. The lower repository
accepts the database's closed capability vocabulary. No production API, CLI, startup
seed or revocation service invokes the seam; no weak administrative API is added here.

granted_by_user_id is nullable caller-supplied FK metadata, not authenticated HUMAN
provenance. It must never be promoted into an audit actor. Existing `nexus_runtime`
privileges permit SELECT/INSERT/UPDATE on `platform_grants`, not DELETE. Schema owner
`nexus_migration`, privileged DBA SQL and test fixtures can mutate grants independently.
These manual/bootstrap/test operations are outside certified application audit capture.
This limitation covers all capabilities in the same table, including
`audit:platform:read` and `audit:platform:verify`; it is not a selective audit exemption.

Live platform query authority still checks explicit grants and current user/session
state. A revocation test proves access denial, not a fabricated revocation audit fact.
Future authenticated grant administration must register its own producer, narrow source
authority, action/target/actor/metadata contracts and same-transaction durable intent.
The generic ledger does not require redesign for that producer. No unrestricted PLATFORM
write privilege is granted to tenant runtime. Static call-site guards detect newly
introduced administrative paths for review; they are not a sandbox against arbitrary
Python or a hostile database administrator rewriting the system.

### Historical deferred design and remaining coverage

P22-SD01's "Privileged platform-global audit authority" gap is historical SUPERSEDED:
durable ingestion, global provenance, append-only ledger, independent ACL and integrity
now belong to P22. General raw Sentinel diagnostics and global unauthenticated login
failures remain outside registered coverage unless individually integrated and proven.
They are not implicitly certified. Remaining gaps and future retention authority remain
visible for P27/P30 planning. No retrospective facts are fabricated.

## Concurrency and failure certification

Real PostgreSQL deterministic barriers/transactions, not sleep-only simulations:
C01 same-tenant appends; C02 different tenants; C03 redelivery; C04 concurrent duplicate;
C05 semantic replay conflict; C06 post-insert/pre-ACK crash; C07 lost ACK; C08 chain
assignment; C09 stale predecessor; C10 verification during append; C11 chain isolation;
C12 foreign record ID; C13 pagination during append; C14 correction/original race;
C15 source rollback/intent; C16 committed outbox/consumer; C17 revoked membership and
historical fact; C18 same actor across tenants; C19 poison retry/terminal; C20 recursion.

Additional PLATFORM PostgreSQL tests prove independent tenant/platform append,
concurrent append and replay, modified replay rejection, source rollback/intent atomicity,
worker crash/recovery, immutability and role separation, platform query grants, and
rejection of non-null Organization attribution.

Corrective cases P01–P10 additionally prove independent platform domains, same-domain
contention, concurrent claims, duplicate convergence, crash rollback recovery, invalid
old ownership after rollback (no leases), poison isolation, multi-domain tamper detection,
tenant-lock independence and platform-lock independence. Real PostgreSQL transactions
and deterministic barriers establish authority; process-local mocks do not.

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
AC41 explicit supported/deferred Sentinel inventory and no unproven universal claim;
AC42 no global Sentinel
attribution to a login/arbitrary Organization; AC43 no misrepresentation of direct P04
GLOBAL publication as transactional durable audit capture.

AC44 disjoint TENANT/PLATFORM scopes; AC45 independent least-privilege platform role;
AC46 durable platform ingestion/idempotent recovery; AC47 append-only independent platform
integrity; AC48 bounded platform-grant queries; AC49 authoritative Sentinel privileged
facts without tenant attribution.

AC50 no singleton PLATFORM append lock; AC51 deterministic verifiable independent domains;
AC52 useful multi-worker concurrency; AC53 finite extensible producer authority;
AC54 unknown/forged producers rejected; AC55 complete grant mutation authority inventory
without fabricated admin coverage; AC56 canonical P20 phase/requirement dependency.

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
