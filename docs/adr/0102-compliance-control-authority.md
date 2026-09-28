# ADR-0102 — Tenant compliance control authority

Status: accepted for P21 admission; implementation and certification pending.

## Context

P21 originally registered only dependency P03 and lacked a mandatory requirement and manifest.
The Master Orchestrator authorizes NXS-COMP-001 and the admission package before the
canonical start. P20 is already canonical READY/GO after PR #46; Sentinel remains
platform SRE authority, not a tenant compliance authority.

## Decision

P21 directly requires P03 and P06. NXS-COMP-001 additionally binds validated
NXS-CUSTOMER-001 and NXS-EVENT-003, making the consumed customer and transactional
outbox authorities explicit alongside its authentication and tenancy dependencies.

Organization is the only tenant boundary. P21 uses authenticated P03 identity,
explicit organization RBAC, nexus_runtime and forced PostgreSQL RLS. Migrations use
nexus_migration. No new role, synthetic principal or system Organization is introduced.

PostgreSQL owns immutable activated policy revisions, hold state, request verification,
plans, approvals and execution fences. P04 transactional outbox announcements are
observations, not authority. No model makes authoritative compliance decisions.

All P21 policy/hold/approval/planning/execution mutations serialize on an Organization
compliance control row. The order is control row, request, plan, execution, owning
domain target. This deliberately favors a simple correctness boundary over parallel
destructive throughput. Active policy uniqueness and tenant-aware foreign keys provide
additional database defenses. Immutable semantic fingerprints bind policy, target and
hold revisions; any relevant change invalidates authorization.

Only source-registered typed adapters may read or mutate explicitly supported resource
classes. The initial supported scope is the P06 customer profile, not the customer
identity graph, conversation contents or provider-held data. Profile anonymization is
a bounded P06-owned mutation, not a claim of complete subject erasure. Credentials are
never generically exportable. Deferred material domains force PARTIAL results.

The final destructive check and supported local domain effect share one transaction;
there is no external I/O within it. A future external adapter requires a separately
reviewed dispatch/ambiguity protocol; it cannot be selected through request data.

## Consequences and limits

Legal hold overrides erasure/anonymization/retention deletion. Hold release and policy
activation are durable authenticated privileged decisions. Destructive plans additionally
require exact-bound unexpired approval, current policy, current hold epoch and target
version. Stable operation identity and generation fencing reject duplicate/stale work.

P16 exclusively owns campaign consent, suppression and send eligibility. A P21 profile
restriction is not a global campaign restriction; unsupported restriction coverage is
reported explicitly. P08 tools, P14 workflows, P15 scheduling and P20 SRE retain their
authority. P21 supplies neither P22's enterprise audit ledger nor legal advice,
legislation interpretation, regulatory certification, deployment or failover.

See the P21 design for the inventory, lifecycle, negative cases and certification map.
