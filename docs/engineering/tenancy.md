# Multi-tenancy and Organizations

The customer-facing tenant is an **Organization**; internally it is a **tenant**; its
canonical identifier is `organization_id` (UUIDv7). No other tenancy key exists.

## Threat model

| Threat | Prevention | Detection / evidence |
| --- | --- | --- |
| Tenant id spoofing (header/body/query/path) | Production resolves no trusted context; test header resolver rejected in staging/prod; body `organization_id` is an unknown field | `tests/security/test_tenant_security.py` |
| A query omits `WHERE organization_id` | Forced RLS `USING`/`WITH CHECK` on the transaction-local GUC | `tests/integration/test_rls_isolation.py` |
| Runtime role turns RLS off / drops policy / superuser / BYPASSRLS | `nexus_runtime` is `NOSUPERUSER NOBYPASSRLS`, no ownership, no DDL; startup fails closed in staging/prod | `tests/integration/test_runtime_role_security.py` |
| Table owner bypasses RLS | `FORCE ROW LEVEL SECURITY` on every tenant table | schema guard live check |
| Connection-pool context leakage | `set_config(..., is_local => true)` — scope dies with the transaction | `tests/integration/test_pool_isolation.py` (pool_size=1) |
| Async ContextVar leakage | per-task `TenantContext`; `bind_tenant` on the request path (no cross-context token) | `tests/concurrency/test_tenant_concurrency.py` |
| Mass-assignment of `organization_id` | tenant repositories operate on the current Organization only; no `get(arbitrary_id)` | `domain/organizations/repository.py` |
| Cross-tenant resource id probing | RLS returns not-found, never another tenant's row | `test_spoofed_but_unknown_org_is_not_found` |
| Cache namespace collision | `tenant_namespace(organization_id, ...)` — no global fallback | `tests/unit/test_tenancy_primitives.py` |
| Future message/job context loss | `TenantContext.to_portable()` / `from_portable()` | ADR-0036; `docs/engineering/tenancy.md` P04 seam |
| Suspended/archived Organization access | `require_operational` / `update_profile` raise `NXS_ORG_INACTIVE` | `tests/integration/test_organization_lifecycle.py` |
| Unsafe future tenant table | `scripts.nxs_schema_guard` CI gate | `tests/unit/test_schema_guard_static.py` |

## Database roles

Provision with `make db-bootstrap` (wraps `scripts.nxs_dbadmin` → `infrastructure/postgres/roles.sql`).
A fresh compose volume provisions them automatically via initdb.

- `nexus_migration` — owns schema, runs Alembic (`NXS_DATABASE__MIGRATION_DSN`). Not superuser, cannot bypass RLS.
- `nexus_runtime` — the application (`NXS_DATABASE__DSN`). `NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB`, `USAGE` on schema, per-table `SELECT/INSERT/UPDATE` only.

`make migrate` / `make migrate-check` use the migration role; the app and the test suite use the runtime role.

## Writing a tenant-scoped table (future phases)

```python
class Widget(TenantOwnedMixin, Base):
    __tablename__ = "widgets"
    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint("organization_id", "external_id"),
    )  # tenant-local uniqueness
```

In the migration:

```python
from nexus_ai.infrastructure.rls import apply_tenant_rls

op.create_table("widgets", ...)
apply_tenant_rls(op, "widgets")  # ENABLE + FORCE RLS, policy, runtime grants
```

Rules for every tenant table: `organization_id NOT NULL` FK to `organizations.id`, no
cross-tenant FK (both sides same Organization), tenant-aware uniqueness
(`UNIQUE(organization_id, ...)`), tenant-aware indexes, RLS enabled **and** forced,
`USING` and `WITH CHECK` policies. CI's schema guard fails the build otherwise.

## Tenant code paths

```python
@router.get("/widgets")
async def list_widgets(context: TenantContextDep, session: TenantSessionDep) -> list[Widget]:
    # session.session is RLS-scoped to context.organization_id
    return await WidgetRepository(session).all()
```

- `TenantContextDep` — resolves the trusted context, binds `organization_id` into the log context.
- `TenantSessionDep` — opens `tenant_transaction`, yields the scoped `TenantSession`.
- `OrganizationServiceDep` — the domain service (owns lifecycle rules).

The P01 `DbSessionDep` remains for SYSTEM/infrastructure use and is NOT tenant-scoped.

## P03 seam

P03 replaces `NullTenantContextResolver` with an authenticated resolver: principal →
allowed Organization → `TenantContext`. No other P02 tenancy code changes.

## P05 seam

P05 calls `OrganizationService.create_core_record` (→ `PROVISIONING`), establishes tenant
scope, provisions resources, then `transition(id, ACTIVE)`. `NXS-ORG-001` (automatic
provisioner, admin, quotas, dashboards, channels, billing) stays PLANNED for P05.

## P04 seam

`TenantContext` is a plain serialisable value (`to_portable`); P04 events and jobs carry
`organization_id` explicitly rather than relying on process-local `ContextVar`.
