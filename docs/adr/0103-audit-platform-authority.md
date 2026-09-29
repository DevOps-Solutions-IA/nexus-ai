# ADR-0103 — Dual-scope audit recording and integrity authority

Status: accepted implementation contract under Master Orchestrator authorization;
The dual-scope decision supersedes historical P22-SD01. Not implementation certification.

## Decision

P22 records security-sensitive actions under explicit TENANT or PLATFORM scope.
Organization is the sole tenant owner; PLATFORM organization_id must be null.
Human, AI-agent, system and service actors obtain
identity from trusted source boundaries, never client-supplied audit fields. P03 owns
authentication/authorization; P04 owns transport/outbox; domain phases own business
semantics; P21 owns compliance decisions. Audit facts never grant those authorities.

Use a per-tenant PostgreSQL serialized SHA-256 chain, append-only runtime records,
forced RLS, tenant-safe foreign keys, deterministic semantic replay checks and bounded
verification/query APIs. A separate platform ledger/integrity domain uses dedicated
least-privilege authority; no global lock serializes tenant appends.
Tenant source mutation and P04 outbox intent are atomic where local;
ledger consumption is separately durable/eventual and idempotent. Do not claim atomicity
with an external provider effect. No arbitrary ingestion, metadata blobs, SQL, secrets,
raw prompts, mutation API or model authority is introduced.

Tenant audit authority != platform-global operator audit authority. P22 does not convert
global platform facts into tenant-owned facts merely to fit its schema. It fails closed
rather than inventing an Organization. ADR-0101 remains unchanged: Sentinel is not a
tenant principal; diagnostic Organization references and an operator's login Organization
do not own platform-global actions. No tenant context is synthesized around
SentinelDatabase, and nexus_sentinel stays separate from nexus_runtime tenant authority.

The dual-scope decision requires authoritative Sentinel privileged approvals/executions,
kill-switch and risk-policy mutations to produce PLATFORM audit facts where observable.
Only Sentinel tenant-owned business actions are NOT_APPLICABLE: P20 owns no such authority.
P04 publish_global is direct confirmed publication, not transactional durable audit capture.

Append a typed immutable platform source intent in the same nexus_sentinel transaction
as each supported local mutation. The source role receives only narrow intent INSERT
authority, not platform ledger access. A dedicated nexus_audit_platform worker persists
the platform ledger and processing receipt idempotently from durable intents. It has no
tenant access, superuser, BYPASSRLS, role membership or schema-DDL authority. Its credential
is never injected into tenant request handling. Separate bounded platform queries require
live authentication and an explicit platform grant, not a tenant capability.

Source mutation and platform intent are atomic locally; subsequent ledger processing is
durable/eventual. The platform source journal bridges absent tenant-outbox ownership; it
is not another generic broker. External effects remain non-atomic with PostgreSQL and
must preserve ambiguous outcomes. GLOBAL envelopes may be reused without attributing
durability to direct publication alone.

## Consequences

Only proven supported producer/action families in each scope may be claimed as audited.
There is no unproven universal platform/Sentinel/privileged/enterprise coverage claim.
Unsupported families remain explicit in the producer inventory. Historical P22-SD01 is
preserved as SUPERSEDED, not erased or treated as current scope.
No regulatory certification, infinite retention or protection against a hostile database
owner rewriting all facts and anchors is claimed. P22 has no destructive audit retention.

See `../engineering/nxs-p22-audit-design.md` for producer inventory, limits, failure
semantics, C01-C20, AC01-AC43 and certification obligations. Stage A remains
BUILDING/PENDING until separately authorized external implementation audit and Stage B.
