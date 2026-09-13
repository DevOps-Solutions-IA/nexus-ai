# NXS-P14 Readiness

NXS-P14 implements the durable Workflow Engine required by `NXS-WF-001`. The implementation commit is `62d57ea1a5f4e6d2e52567809d85a2aa7b23c7bc`; canonical NXS closure records `READY / GO`, clears the active phase and execution lock, and computes NXS-P15 as the next eligible phase without activating it.

## Delivered boundary

- PostgreSQL-authoritative definitions, immutable versions, runs, step runs, claims, bounded retries, cancellation, pause/resume, and transition history.
- Forced RLS and tenant-aware composite foreign keys on all six workflow tables.
- `TOOL` execution only through NXS-P08 and `AGENT` execution only through NXS-P13.
- Constrained deterministic `CONDITION` and `NOOP` steps; arbitrary HTTP, provider, shell, SQL, cron, campaign, and human-queue steps are rejected.
- P04 transactional workflow events, existing RFC 9457 errors, existing authorization architecture, and bounded `/api/v1` contracts.
- Durable stale/ambiguous execution evidence without automatic reaping or reassignment; NXS-P25 owns recovery.

## Validation

- Canonical quality gate: 1,619 passed, zero failed, 91.84% coverage.
- P14-focused tests: 66 across unit, integration, contracts, security, concurrency, and resilience.
- Distributed concurrency matrix: ten PostgreSQL-backed scenarios.
- Clean-room: frozen installation, fresh migrations, schema guard, full suite, non-root runtime, graceful shutdown, and amd64/arm64 builds pass.
- Candidate exact-head NXS CI and NXS Security pass on push and pull-request events.

Machine-readable evidence is authoritative under `.nxs/evidence/NXS-P14/`. Merge, NXS-P15 implementation, and deployment remain unauthorized.
