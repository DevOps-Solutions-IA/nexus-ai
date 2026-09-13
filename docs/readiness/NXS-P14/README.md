# NXS-P14 Readiness

NXS-P14 implements the durable Workflow Engine required by `NXS-WF-001`. Corrective 01 fixes conditional exclusion propagation. Corrective 02 makes mandatory conjunction and alternative convergence explicit through immutable `ALL`/`ANY` dependency modes with a fail-closed `ALL` default. The validation candidate is `f87cf174182e863afec83cae871ed9a76397f7ed`; canonical re-closure records `READY / GO`, clears the active phase and execution lock, and computes NXS-P15 as the next eligible phase without activating it.

## Delivered boundary

- PostgreSQL-authoritative definitions, immutable versions, runs, step runs, claims, bounded retries, cancellation, pause/resume, and transition history.
- Forced RLS and tenant-aware composite foreign keys on all six workflow tables.
- `TOOL` execution only through NXS-P08 and `AGENT` execution only through NXS-P13.
- Constrained deterministic `CONDITION` and `NOOP` steps; arbitrary HTTP, provider, shell, SQL, cron, campaign, and human-queue steps are rejected.
- Fixed-point conditional propagation evaluates each immutable version step as `ALL` or `ANY`: ordinary dependencies require every predecessor to complete, while explicit alternative joins wait for every branch to resolve and require at least one completed predecessor.
- P04 transactional workflow events, existing RFC 9457 errors, existing authorization architecture, and bounded `/api/v1` contracts.
- Durable stale/ambiguous execution evidence without automatic reaping or reassignment; NXS-P25 owns recovery.

## Validation

- Canonical quality gate: 1,649 passed, zero failed, 91.90% coverage.
- P14-focused tests: 96 across unit, integration, contracts, security, concurrency, and resilience.
- Distributed concurrency matrix: eleven PostgreSQL-backed scenarios, including contradictory condition completion.
- Clean-room: frozen installation, fresh migrations, schema guard, full suite, non-root runtime, graceful shutdown, and amd64/arm64 builds pass.
- Candidate exact-head NXS CI and NXS Security pass on push and pull-request events for `f87cf174182e863afec83cae871ed9a76397f7ed`.

Machine-readable evidence is authoritative under `.nxs/evidence/NXS-P14/`. Merge, NXS-P15 implementation, and deployment remain unauthorized.
