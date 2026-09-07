# Branch Contract

Every execution branch must define: phase ID, objective, requirement IDs, dependencies and preconditions; scope and non-scope; functional, security, concurrency, resilience and test criteria; evidence paths; rollback and observability; GO/NO-GO decision; and an objective READY definition.

## NXS-P00 contract

- Phase: NXS-P00; branch: `feat/nxs-p00-engineering-control-system`.
- Objective: permanent agent-neutral governance, evidence, CI, security, deterministic runtime and validation.
- Preconditions: empty private canonical repository, initialized `main`, no prior P00 READY state.
- Scope: manifest requirements and minimal health/version runtime only. Business capabilities and production deployment are excluded.
- Criteria: every mandatory manifest gate passes; malformed and conflicting execution states block; container runs non-root and healthy; an independent agent reconstructs status from repository files.
- Rollback: abandon the unmerged feature branch; `main` retains repository identity only.
- Observability: structured runtime lifecycle logs and health/version endpoints.
- READY: requirements VALIDATED, evidence present, implementation SHA recorded, status READY and decision GO.

## NXS-P01 contract

- Phase: NXS-P01; branch: `feat/nxs-p01-backend-core`; depends on NXS-P00 (READY/GO, merged at `73b399f`).
- Objective: make the engineering lifecycle generic for every registry phase, then establish the permanent production Backend Core.
- Requirements: `NXS-PLATFORM-002`, `NXS-PLATFORM-003`, `NXS-CICD-001`, `NXS-CONFIG-001`, `NXS-API-001`, `NXS-ERROR-001`, `NXS-CTX-001`, `NXS-LOG-001`, `NXS-TELEMETRY-001`, `NXS-HEALTH-001`, `NXS-DATA-001`, `NXS-CACHE-001`, `NXS-EVENT-001`, `NXS-MIGRATE-001`.
- Preconditions: `main` synchronized, guard PASS on the P01 branch, no conflicting active phase.
- Scope: generic phase start/closure/next-phase, executable lock CLI, phase-neutral CI, unified Python 3.14 toolchain, application factory, validated configuration, `/api/v1` foundation, Problem Details errors, request/correlation context, structured redacted logging, telemetry boundary, async PostgreSQL/Valkey/NATS foundations, liveness/readiness, Alembic framework.
- Non-scope: organizations, tenancy, auth/OTP, CRM/ERP, conversations, channels, telephony, ElevenLabs, agent runtime, workflows, campaigns, human agents, cell scaling, production deployment, business tables and event contracts.
- Security: secrets never in `repr`/logs; DSNs/URLs redacted; Authorization and Cookie headers never logged; trusted-host and default-deny CORS; no wildcard CORS in production; no stack traces in responses; SHA-pinned third-party actions; least-privilege workflow permissions.
- Concurrency: request/correlation ids never bleed between concurrent tasks; DB sessions independent; errors stay correlated (proven under 200-way concurrency).
- Failure scenarios: PostgreSQL / Valkey / NATS unavailable → live 200, ready 503, recovery without restart; invalid production configuration → startup refused; unhandled exception → safe Problem Details + correlated internal log.
- Tests: `tests/unit`, `tests/contracts`, `tests/security`, `tests/concurrency`, `tests/resilience`, `tests/integration` (real PostgreSQL/Valkey/NATS); ≥90% branch-aware coverage on active code.
- Evidence: `.nxs/evidence/NXS-P01/`; human summary in `docs/readiness/NXS-P01/`.
- Rollback: abandon the unmerged branch; `main` stays at the certified P00 state. Alembic ships no versions, so there is nothing to roll back; the first migration will carry its own notes.
- Observability: startup/shutdown events logged, dependency state changes visible, errors carry request/correlation ids, secrets redacted, readiness reports dependency state safely.
- READY: P00 still READY/GO; lifecycle generic with no hardcoded P01; toolchain consistent; factory + configuration + API v1 + Problem Details + isolated context + structured logging + PostgreSQL/Valkey/NATS + liveness/readiness + graceful shutdown + non-root image + security gates + clean-room + green GitHub CI all pass; P01 requirements VALIDATED; agent handoff resolves P02 next; project state says P01 READY/GO with `next_allowed_execution = NXS-P02`; no merge to `main`.
