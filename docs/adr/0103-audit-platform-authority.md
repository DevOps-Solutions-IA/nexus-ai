# ADR-0103 — Tenant audit recording and integrity authority

Status: accepted implementation contract under Master Orchestrator authorization;
P22-SD01 explicitly resolves platform-global coverage. Not implementation certification.

## Decision

P22 records security-sensitive actions whose authoritative scope is tenant-owned.
Organization is the sole tenant owner. Human, AI-agent, system and service actors obtain
identity from trusted source boundaries, never client-supplied audit fields. P03 owns
authentication/authorization; P04 owns transport/outbox; domain phases own business
semantics; P21 owns compliance decisions. Audit facts never grant those authorities.

Use a per-tenant PostgreSQL serialized SHA-256 chain, append-only runtime records,
forced RLS, tenant-safe foreign keys, deterministic semantic replay checks and bounded
verification/query APIs. Source mutation and P04 outbox intent are atomic where local;
ledger consumption is separately durable/eventual and idempotent. Do not claim atomicity
with an external provider effect. No arbitrary ingestion, metadata blobs, SQL, secrets,
raw prompts, mutation API or model authority is introduced.

Tenant audit authority != platform-global operator audit authority. P22 does not convert
global platform facts into tenant-owned facts merely to fit its schema. It fails closed
rather than inventing an Organization. ADR-0101 remains unchanged: Sentinel is not a
tenant principal; diagnostic Organization references and an operator's login Organization
do not own platform-global actions. No tenant context is synthesized around
SentinelDatabase, and nexus_sentinel stays separate from nexus_runtime tenant authority.

P22-SD01 explicitly defers Sentinel global observations requiring a global ledger,
privileged approvals/executions, kill-switch and risk-policy mutation. These known gaps
are DEFERRED, not SUPPORTED or NOT_APPLICABLE. Only Sentinel tenant-owned business
actions are NOT_APPLICABLE because P20 owns no such authority. P04 publish_global is
direct confirmed publication, not transactional durable audit capture. A future global
audit boundary needs separate durable ingestion, access, integrity and retention design.

## Consequences

Only supported tenant action families may be claimed as audited. There is no universal
platform/Sentinel/privileged/enterprise coverage claim. The durable deferred-design entry
in the P22 design remains visible for security-hardening/backend-certification planning.
No regulatory certification, infinite retention or protection against a hostile database
owner rewriting all facts and anchors is claimed. P22 has no destructive audit retention.

See `../engineering/nxs-p22-audit-design.md` for producer inventory, limits, failure
semantics, C01-C20, AC01-AC43 and certification obligations. Stage A remains
BUILDING/PENDING until separately authorized external implementation audit and Stage B.
