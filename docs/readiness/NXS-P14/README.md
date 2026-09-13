# NXS-P14 Readiness

NXS-P14 implements the durable Workflow Engine required by `NXS-WF-001`. Corrective 01 fixes conditional exclusion propagation and certifies deterministic convergence. The validation candidate is `85a7dabb046f0e56adc2d6fe54a7a9f0f92ddd55`; canonical re-closure records `READY / GO`, clears the active phase and execution lock, and computes NXS-P15 as the next eligible phase without activating it.

## Delivered boundary

- PostgreSQL-authoritative definitions, immutable versions, runs, step runs, claims, bounded retries, cancellation, pause/resume, and transition history.
- Forced RLS and tenant-aware composite foreign keys on all six workflow tables.
- `TOOL` execution only through NXS-P08 and `AGENT` execution only through NXS-P13.
- Constrained deterministic `CONDITION` and `NOOP` steps; arbitrary HTTP, provider, shell, SQL, cron, campaign, and human-queue steps are rejected.
- Fixed-point conditional propagation skips every all-excluded descendant while allowing a convergence node only after all predecessors resolve and at least one predecessor completed.
- P04 transactional workflow events, existing RFC 9457 errors, existing authorization architecture, and bounded `/api/v1` contracts.
- Durable stale/ambiguous execution evidence without automatic reaping or reassignment; NXS-P25 owns recovery.

## Validation

- Canonical quality gate: 1,638 passed, zero failed, 91.91% coverage.
- P14-focused tests: 85 across unit, integration, contracts, security, concurrency, and resilience.
- Distributed concurrency matrix: eleven PostgreSQL-backed scenarios, including contradictory condition completion.
- Clean-room: frozen installation, fresh migrations, schema guard, full suite, non-root runtime, graceful shutdown, and amd64/arm64 builds pass.
- Candidate exact-head NXS CI and NXS Security pass on push and pull-request events.

Machine-readable evidence is authoritative under `.nxs/evidence/NXS-P14/`. Merge, NXS-P15 implementation, and deployment remain unauthorized.
