# Nexus AI Agent Protocol

This file is the mandatory universal entrypoint for Codex, Claude Code, DeepSeek CLI, Gemini CLI, and future coding agents.

## Authority

Do not trust conversational memory. GitHub, Git history, and machine-readable NXS state are authoritative, in that order. Never infer a READY state or fabricate evidence.

## Documentation synchronization rule

`README.md` must evolve with the canonical product state. At the closure of every material phase, architecture-boundary change, certification change, release-status change, or other user-visible capability change, the acting agent must evaluate whether the README is now materially stale and update it when required.

The README must describe only capabilities and maturity that are supported by canonical `main` and NXS evidence. Work that exists only on a feature/governance branch must be labeled as in progress, planned, or pending merge; it must never be presented as canonically implemented. Machine-readable `.nxs/` state remains authoritative when README prose and execution state differ.

A materially stale README is a documentation defect. Prefer updating it in the same governed change when the phase closure or architecture change makes the new status known; otherwise create a dedicated documentation PR before the project advances far enough for repository-facing status to become misleading. README synchronization does not bypass normal merge authorization, CI/security checks, semantic review, or exact-main verification.

## Before implementation

1. Read this file.
2. Read `.nxs/project-state.json`.
3. Run `make nxs-validate-repo` to validate schemas and invariants.
4. Read `.nxs/execution-policy.json`.
5. Read `.nxs/requirements.json`.
6. Read `.nxs/phase-registry.json`.
7. Read the requested `.nxs/phases/<phase>.json` manifest.
8. Run `make nxs-preflight PHASE=<phase>` and continue only on PASS.
9. Inspect relevant records in `docs/adr/`.
10. Inspect the branch, HEAD, remotes, history, and working tree.
11. Implement only within the manifest and branch contract.

Begin an eligible phase with `make nxs-start PHASE=<phase> ACTOR=<agent>`; it validates the guard, maps the phase requirements, transitions to `BUILDING`, sets `active_phase`, and acquires the execution lock. The next eligible phase is computed generically from `.nxs/phase-registry.json` and dependency state — no phase identifier is hardcoded anywhere in the control system.

Status changes must use `python -m scripts.nxs_state`; the execution lock is operated through `python -m scripts.nxs_guard lock {acquire|status|release|recover}` (or the `make nxs-lock-*` targets); closure uses `make nxs-close PHASE=<phase> IMPLEMENTATION_COMMIT=<sha>`. A mandatory failed gate prevents READY/GO. Never merge `main`, deploy production, rewrite history, expose secrets, or bypass a gate without explicit human authorization and documented risk acceptance.

Read `docs/runbooks/phase-lifecycle.md` before starting a phase or acquiring or recovering a lock. Future component directories marked `PLANNED / NOT IMPLEMENTED` are architectural reservations, not working software.

Tenant isolation is a PostgreSQL boundary, not an application convention: every tenant-scoped table has forced Row-Level Security, the application connects as the non-bypass `nexus_runtime` role, and `scripts.nxs_schema_guard` fails CI on an unsafe tenant table. Run `make db-bootstrap` before migrating; use `NXS_DATABASE__MIGRATION_DSN` (role `nexus_migration`) for schema changes and `NXS_DATABASE__DSN` (role `nexus_runtime`) for the app and tests. See `docs/engineering/tenancy.md`.
