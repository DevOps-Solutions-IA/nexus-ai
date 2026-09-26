# P20-B foundation work package

P20 is BUILDING / PENDING on `feat/nxs-p20-sentinel`; this is not phase
certification. Governance was PLANNED before the canonical start on
2026-09-25. No dispatch, model execution, operator API, closure, merge or
deployment is part of this slice.

## Authority and transaction design

Sentinel rows are platform control data. An affected Organization UUID is
diagnostic metadata, not a foreign-key capability, tenant context or query grant.
`nexus_sentinel` has its own connection and explicit object privileges. No tenant
GUC is installed. Migration ownership stays with `nexus_migration`.

Linearization points, before implementation:

| Operation | Authority and winner |
| --- | --- |
| Signal ingestion | Unique adapter/source/observation identity; conflict waits for the inserting transaction and compares the complete semantic digest. Receipt and correlation commit together. |
| Incident correlation | Unique digest of source class, subject kind/id and incident fingerprint; conflict locks the existing incident. Its revision and receipt link commit together. |
| Runbook revision | Unique stable key/revision and immutable definition; conflicting content is rejected. Database UPDATE/DELETE protection preserves old revisions. Source registry, not database text, defines handlers. |
| Proposal convergence | Incident lock, immutable runbook snapshot, policy snapshot, unique semantic digest; the committing insert wins. No external effect occurs. |
| Policy mutation | Singleton control row, expected revision CAS; revision advances exactly once. |
| Kill switch mutation | Same control-row CAS as policy; approvals cannot override it. |

Incident mutation uses expected revision under a row lock. Resolution requires an
explicit trusted recovery or operator decision, not elapsed time. Findings and
approvals are append-only. Proposal semantics are immutable; new semantics create
a new proposal and approval identity. Execution records are schema foundations,
not dispatch grants. No method in this slice sends an external action.

Internal persistence calls assume trusted platform code, not a tenant endpoint.
Authenticated operator admission and the final execution/lease fence remain
later P20 work packages. Persisted approval alone is never execution permission.
No P08/P13 runtime, tenant credentials, arbitrary SQL/shell/URL adapter or fake
Organization is used.

## Local foundation surface

`nexus_ai.sentinel` contains strict contracts, bounded settings, an independent
database connection, persistence operations and risk assessment. Domain metadata
lives under `nexus_ai.domain.sentinel`. The eight `sentinel_*` tables have forced
RLS restricted to the dedicated identity. Explicit grants revoke the normal
runtime's inherited migration defaults. No Sentinel default tenant grants exist.
The local bootstrap provisions the role; Alembic never creates a role or expands
the migration role's privileges.

`SentinelDatabase.transaction()` checks both current and session user, unsafe role
flags, schema CREATE, inherited role memberships and contamination by tenant GUC.
It never installs tenant context. Settings use `NXS_SENTINEL__...`, including a
separate `DATABASE_DSN` SecretStr, and never fall back to the application DSN.
Local/test credentials follow `infrastructure/postgres/roles.sql`; production
secrets are not supplied by this work package.

Signals are accepted only from constructor-supplied trusted source registrations
binding adapter/revision/source and exact subject metadata. Facts currently have
only condition, bounded count and latency; raw logs, arbitrary JSON, URLs and
credentials are not accepted. Finding evidence references must identify receipts
of that same incident. Findings remain advisory and their explanation must be
sanitized by trusted callers; this is not a live model integration.

Runbook rows identify immutable revision records; `key` is the stable family
identity and each revision has its own UUIDv7. Definitions must exactly match the
trusted source registry. That registry contains metadata only in this slice, not
executable adapters. Typed parameters are deliberately limited to sample/window
bounds. No database row can create a handler.

Proposal semantics are immutable: changing them creates a distinct proposal.
Canonical JSON sorts keys, rejects non-finite numbers and normalizes timestamps
to UTC before hashing SHA-256. The digest also binds creator type and expiry.
Approvals are append-only decisions, unique per proposal/operator, limited to 16
operators, with a composite FK binding fingerprint and policy revision. Exact
current incident/runbook/policy/target-generation and DB-time expiry checks are
required by the eligibility assessment. A rejection vetoes that assessment.

`eligible()` is **not** a dispatch grant. Operator authentication/RBAC admission,
final target admission and lease/dispatch fencing must be implemented and audited
in later P20 work packages before any external effect. `record_approval()` and
resolution markers are trusted internal persistence seams, never raw model,
signal or tenant inputs. No public/operator HTTP endpoint is introduced here.

The execution table reserves one durable slot per proposal, generation, owner,
bounded identity columns, lease and dispatch/result states. `nexus_sentinel` has
SELECT only on that table in this slice: no execution claim or dispatch can occur.
The mutable-action control starts disabled in PostgreSQL; config cannot override
that kill switch. Observation and correlation remain available while disabled.

## Validation and lifecycle boundary

Foundation tests and evidence use F-C01–F-C16 and F-AC01–F-AC36, not the full P20
phase matrix. Disposable migration tests cover fresh and P19-head upgrades,
downgrade, re-upgrade, Alembic metadata parity and the existing tenant schema
guard. No historical migration is edited.

The existing OTP future-phase contract previously required P20 to stay PLANNED.
Its only lifecycle adjustment now asserts P20 BUILDING/PENDING and NXS-SRE-001
IN_PROGRESS explicitly; all P21–P32 PLANNED/PENDING assertions remain intact.
This does not weaken OTP, authentication or future-phase boundaries.
