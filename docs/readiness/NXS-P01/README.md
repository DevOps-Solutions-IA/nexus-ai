# NXS-P01 Readiness — Production Backend Core

Branch `feat/nxs-p01-backend-core`, dependency NXS-P00 READY/GO (merged to `main` at
`73b399f`). Machine-readable evidence in `.nxs/evidence/NXS-P01/` is authoritative for
automated gates; this page is the engineering summary for independent audit.

## What was delivered

**Generic engineering lifecycle.** The audited P00 continuity defects are remediated:
`scripts.nxs_start` begins any eligible phase; the closure engine computes
`next_allowed_execution` from the registry DAG (deterministic registry-order tie-break)
and replaces the full `current_phase` identity; the execution lock has an `acquire /
status / release / recover` CLI; CI, the Makefile and the toolchain are phase-neutral.
No phase identifier is hardcoded in the control system. Thirteen state-machine scenarios
plus control-CLI tests cover it (`tests/unit/test_lifecycle.py`, `test_control_core.py`,
`test_control_cli.py`). ADR-0028.

**Toolchain.** Single Python 3.14 baseline across `requires-python`, Ruff
`target-version`, mypy, the CI matrix, `uv` and the container image; Ruff 0.16.6 and
Bandit 1.9.2 for 3.14 support; lock regenerated; clean-room install verified. ADR-0020.

**Backend Core.** `create_app()` application factory with a typed lifespan and no
import-time connections (ADR-0021); `pydantic-settings` configuration with bounded
values, `SecretStr` secrets, redaction and production fail-fast (ADR-0022); `/api/v1`
surface with safe service metadata; RFC 9457 Problem Details for every error path with
stable `NXS_*` codes and no stack traces (ADR-0023); request/correlation ids over
`contextvars`, structured redacted `structlog` logging, vendor-neutral OpenTelemetry
boundary (ADR-0027); async PostgreSQL foundation with session/transaction primitives and
a health probe (ADR-0024); Valkey and NATS/JetStream foundations behind internal
adapters (ADR-0025); typed liveness/readiness subsystem; Alembic framework wired to
validated settings with no versions and no committed credentials (ADR-0026).

## Verification

- 159 tests pass (147 unit/contract/security/concurrency/resilience + 12 integration
  against real PostgreSQL, Valkey and NATS); branch-aware coverage 90.4%.
- Format, lint (Ruff incl. flake8-bandit `S`), strict mypy, Bandit and `pip-audit` all
  green; no unresolved Critical/High findings.
- Failure injection: PostgreSQL / Valkey / NATS unavailable → liveness 200, readiness
  503; recovery without process restart; invalid production configuration refuses
  startup; unhandled exception → safe Problem Details + correlated internal log.
- Concurrency: request/correlation ids never bleed across 200 concurrent requests.
- Production container: non-root `65532`, `/health/live` healthcheck, SIGTERM drains in
  ~0.3 s; readiness 503 without dependencies and 200 with them.
- Clean-room reproduction from a wiped environment passes end to end.

## Non-scope

Organizations, tenancy, auth/OTP, CRM/ERP, conversations, channels, telephony,
ElevenLabs, agent runtime, workflows, campaigns, human agents, cell scaling, production
deployment, and any business-domain tables or event contracts.

## Rollback

Abandon the unmerged branch; `main` stays at the certified P00 state. Alembic ships no
migration files, so there is nothing to roll back.

## GO / NO-GO

Local gates are GO. Final READY/GO is conditional on green GitHub CI against the
implementation and closure commits (`.nxs/evidence/NXS-P01/github-ci.json`,
`closure.json`) and independent audit authorization before any merge.
