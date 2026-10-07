# Nexus AI Agent Protocol

This file is the mandatory universal entrypoint for Codex, Claude Code, DeepSeek CLI, Gemini CLI, and future coding agents.

## Authority

Do not trust conversational memory. GitHub, Git history, and machine-readable NXS state are authoritative, in that order. Never infer a READY state or fabricate evidence.

## Documentation synchronization — mandatory, fail-closed

`README.md` and `infrastructure/README.md` **must remain synchronized** with
canonical machine-readable NXS state on **every pull request, phase transition,
release change and merge**. This is a blocking engineering invariant, not a
best-effort writing task.

Both README files contain exactly one guarded `NXS:DOC_STATUS` section.
Its contents are deterministically generated from
`.nxs/phase-registry.json` and `.nxs/project-state.json`.
The required `state` GitHub check invokes `scripts.nxs_validate`,
which fails closed when the blocks are missing, altered or stale.
It also has a distinct documentation check step in CI.

**Required agent commands before delivery:**

```bash
uv run python -m scripts.nxs_docs --write
uv run python -m scripts.nxs_docs
make nxs-validate-repo
```

On phase/requirement changes, regenerate both documents **in the same
governed change**. Do not manually edit between guarded markers or
suppress the validator. Text outside the generated blocks must not claim
current status contradicted by NXS state; permanent historic narrative
must be explicitly dated or labeled historical.

Documentation on a feature/PR branch reports **that revision's state**,
not canonical `main` until the PR is merged and exact-main gates pass.
Never conflate successful CI, a feature candidate, contractual provider
tests or installed fixtures with actual production deployment.
Manual semantic audit and human-approved merge remain mandatory.
Any missed documentation parity is a blocker, not a deferrable cosmetic edit.

## Architecture quality and durable deferrals

Prefer long-term correctness, durability, security, scalability, recoverability and
explicit authority boundaries over short-term convenience. Never weaken a gate,
assertion, security policy or coverage threshold merely to manufacture green status.
Every accepted deferral needs a concrete owning phase and a durable record in
`.nxs/deferred-obligations.json`, including impact, blocking phases, source references
and proof expected for resolution. Historical readiness is preserved; unresolved
production blockers prevent future Backend Certification through machine-readable
certification policy. A resolved obligation must bind its completed owner's actual
implementation and evidence. Fixed-duration sleeps cannot prove happens-before in
concurrency tests; use observable semantic boundaries. Dependency security is
time-sensitive and must be checked against the candidate's resolved environment.

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
