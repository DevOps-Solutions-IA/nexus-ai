# NXS-P02 Readiness — Multi-tenancy and Organizations Core

Branch `feat/nxs-p02-tenancy`, dependencies NXS-P00 + NXS-P01 READY/GO (P01 merged to
`main` at `1cc4e28`). Machine-readable evidence in `.nxs/evidence/NXS-P02/` is
authoritative for automated gates; this page is the engineering summary for independent
audit.

## Architecture

The customer term is **Organization**, the internal term **tenant**, the canonical id
`organization_id` (server-generated UUIDv7). The Organization aggregate carries a
reserved-safe `organization_key`, validated profile (display/legal name, ISO country,
IANA timezone, industry code), an internal-only `tax_identifier`, a lifecycle status and
an optimistic-concurrency `version`. The first real domain migration creates the
`organizations` table.

## RLS model

Every tenant-scoped table has `ENABLE ROW LEVEL SECURITY` **and**
`FORCE ROW LEVEL SECURITY`, with `USING` and `WITH CHECK` policies bound to
`nullif(current_setting('nxs.organization_id', true), '')::uuid`. The `organizations`
table is self-scoped (policy on the primary key). A missing tenant context resolves to
`NULL` → zero rows, never a global fallback. `Database.tenant_transaction()` sets the GUC
with `is_local => true` so it dies with the transaction; `apply_tenant_rls` /
`drop_tenant_rls` are the reusable migration helpers; `scripts.nxs_schema_guard` is a
permanent CI gate.

## Database roles

- `nexus_migration` — owns schema, runs Alembic. `NOSUPERUSER NOBYPASSRLS`.
- `nexus_runtime` — the application. `NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB`,
  no ownership, per-table `SELECT/INSERT/UPDATE` only (no `DELETE` on `organizations`),
  no DDL. Startup in staging/production **fails closed** if the connected role reports
  `rolsuper` or `rolbypassrls`.

## Attack matrix (real PostgreSQL, runtime role)

| Attack | Result |
| --- | --- |
| cross-tenant `SELECT` / `UPDATE` / `INSERT` (raw SQL) | blocked (0 rows / error) |
| cross-tenant `DELETE` | no privilege |
| no tenant context | zero visibility; insert blocked |
| pooled connection reuse (commit / rollback / exception, `pool_size=1`) | no scope leak |
| `SET row_security = off`, disable/drop policy, `ALTER`/`DROP TABLE`, `CREATE ROLE` | refused |
| spoofed `X-NXS-Organization-ID` for unknown org | `404 NXS_ORG_NOT_FOUND` (own empty scope) |
| body `organization_id` mismatch (confused deputy) | ignored; `422`; scope unchanged |
| malformed UUID header | `400 NXS_TENANT_CONTEXT_INVALID` |
| 240 concurrent mixed A/B/C operations | zero cross-tenant, ContextVar isolated |
| stale optimistic update | `409 NXS_ORG_VERSION_CONFLICT`, no lost update |
| suspended / archived Organization operation | `403 NXS_ORG_INACTIVE` |

## Verification

- 270 tests (unit, contract, security, concurrency, resilience, integration); branch-aware
  coverage 91.1%.
- Migration: `alembic upgrade head` + `alembic check` clean; `downgrade base` → `upgrade
  head` reversible in an isolated database.
- Multi-arch: production image builds for `linux/amd64` and `linux/arm64`; the emulated
  ARM64 image runs non-root and serves live 200 / ready 503.
- Format, lint (Ruff incl. `S`), strict mypy, Bandit, `pip-audit`, Gitleaks all green.
- Clean-room reproduction (fresh PostgreSQL volume, bootstrapped roles) passes end to end.

## Known limitations / deferred

- No authentication — tenant endpoints return `403 NXS_TENANT_CONTEXT_REQUIRED` in
  production until P03 supplies an authenticated `TenantContextResolver`.
- No automatic provisioner — `create_core_record` creates only the `PROVISIONING` core
  row. `NXS-ORG-001` stays PLANNED for P05.
- No platform Organization administration API (list / arbitrary id) — belongs after P03
  authorization.
- No event contracts — `TenantContext` is serialisable for P04, but P04 owns the wire.

## P03 seam

Bind authenticated principal → allowed Organization → `TenantContext` → `TenantSession`
by replacing `NullTenantContextResolver`. No other P02 tenancy code changes.

## P05 seam

Call `OrganizationService.create_core_record` (→ `PROVISIONING`), establish scope,
provision resources, `transition(id, ACTIVE)`. The lifecycle state machine and
persistence are extended, not replaced.

## GO / NO-GO

Local gates are GO. Final READY/GO is conditional on green GitHub CI against the
implementation and closure commits and independent audit authorization before any merge.
