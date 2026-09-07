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

## NXS-P02 contract

- Phase: NXS-P02; branch: `feat/nxs-p02-tenancy`; depends on NXS-P01 (READY/GO, merged at `1cc4e28`).
- Objective: the permanent multi-tenant security boundary — Organization core domain + strict tenant isolation enforced by PostgreSQL, not application `WHERE` clauses.
- Requirements: `NXS-TENANT-001`..`NXS-TENANT-005`, `NXS-ORG-002`, `NXS-ORG-003`, `NXS-SEC-003`, `NXS-CICD-002`.
- Preconditions: `main` synchronized at P01; guard PASS on the P02 branch; `NXS-ORG-001` remains PLANNED / target NXS-P05.
- Scope: Organization aggregate (UUIDv7 id, reserved-safe key, validated profile, lifecycle state machine, optimistic concurrency), first real migration, forced RLS, transaction-local tenant context, non-bypass `nexus_runtime` role + `nexus_migration` role, tenant unit of work, `TenantOwnedMixin` + schema-guard CI gate, tenant namespace primitive, `TenantContextResolver` boundary, current-Organization API, multi-arch amd64/arm64.
- Non-scope: authentication/RBAC/users (P03); automatic provisioner, admin, quotas, dashboards, channels, billing (P05 / NXS-ORG-001); customers/conversations (P06); events (P04); audit ledger (P22); cell scaling (P18); capacity claims (P28); deployment / Oracle LAB.
- Security: RLS ENABLED + FORCED; runtime role non-superuser, NOBYPASSRLS, no DELETE on organizations, no DDL; startup fails closed in staging/prod if the role can bypass tenancy; header tenant resolver rejected in staging/prod; `tax_identifier` never in public view/repr/logs; no raw DB errors at the API; Gitleaks/Bandit/pip-audit/Trivy pass.
- Concurrency: 240 mixed A/B/C operations with zero cross-tenant reads/writes and ContextVar isolation.
- Failure/attack scenarios: cross-tenant SELECT/UPDATE/INSERT blocked; DELETE unavailable; no-scope → zero visibility; pool reuse across commit/rollback/exception → no leak; spoofed header / body organization_id → no scope switch; stale optimistic update → deterministic conflict; runtime role privilege-escalation attempts fail.
- Tests: `tests/unit`, `tests/contracts`, `tests/security`, `tests/concurrency`, `tests/resilience`, `tests/integration` (real PostgreSQL as `nexus_runtime` with RLS enforced, `nexus_migration` for schema); ≥90% branch-aware coverage; tenant-boundary modules substantially higher.
- Evidence: `.nxs/evidence/NXS-P02/`; human summary in `docs/readiness/NXS-P02/`.
- Rollback: abandon the unmerged branch; `main` stays certified through P01. The organizations migration is reversible in an isolated database (`downgrade base` → `upgrade head` tested); production migrations carry roll-forward notes per `docs/engineering/standards.md`.
- Observability: startup logs the runtime role's bypass capability; `organization_id` enters the log context only after trusted resolution; no high-cardinality tenant metric labels.
- READY: P00 + P01 still READY/GO; P02 started through generic lifecycle; Organization domain + first migration exist; RLS ENABLED + FORCED; runtime role cannot bypass; missing context fails safe; A cannot read/update/insert-as/delete B; pooled connection and async execution cannot leak scope; spoofed identifiers cannot switch scope; lifecycle + optimistic concurrency enforced; tenant-owned pattern reusable + schema validator catches unsafe tables; P03 and P05 seams intact; amd64 + arm64 images build; full regression + coverage ≥90% + security gates + clean-room + green GitHub CI; P02 requirements VALIDATED; `NXS-ORG-001` still PLANNED for P05; project state says P02 READY/GO with `next_allowed_execution = NXS-P03`; no merge to `main`.
