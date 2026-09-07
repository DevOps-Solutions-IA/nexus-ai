"""Row-Level Security primitives for tenant-owned tables (NXS-TENANT-003).

The tenant boundary is enforced by PostgreSQL, not by application ``WHERE`` clauses.
Every tenant-scoped table gets RLS ENABLED and FORCED (owner included) with USING and
WITH CHECK policies bound to a transaction-local GUC. A missing tenant context resolves
to ``NULL`` and therefore to zero visibility — never a global fallback.

``apply_tenant_rls`` / ``drop_tenant_rls`` are used from Alembic migrations so future
phases declare a table tenant-scoped without re-writing security SQL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alembic.operations import Operations

DEFAULT_CONTEXT_SETTING = "nxs.organization_id"
DEFAULT_RUNTIME_ROLE = "nexus_runtime"
POLICY_NAME = "nxs_tenant_isolation"


def tenant_predicate(scope_column: str, setting: str = DEFAULT_CONTEXT_SETTING) -> str:
    """SQL boolean: the row's tenant column equals the transaction-local organization id."""
    return f"{scope_column} = nullif(current_setting('{setting}', true), '')::uuid"


def apply_tenant_rls(
    op: Operations,
    table: str,
    *,
    scope_column: str = "organization_id",
    runtime_role: str = DEFAULT_RUNTIME_ROLE,
    setting: str = DEFAULT_CONTEXT_SETTING,
    allow_delete: bool = False,
) -> None:
    predicate = tenant_predicate(scope_column, setting)
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY "{POLICY_NAME}" ON "{table}" USING ({predicate}) WITH CHECK ({predicate})'
    )
    grants = "SELECT, INSERT, UPDATE" + (", DELETE" if allow_delete else "")
    op.execute(f'GRANT {grants} ON "{table}" TO "{runtime_role}"')


def drop_tenant_rls(
    op: Operations,
    table: str,
    *,
    runtime_role: str = DEFAULT_RUNTIME_ROLE,
) -> None:
    op.execute(f'REVOKE ALL ON "{table}" FROM "{runtime_role}"')
    op.execute(f'DROP POLICY IF EXISTS "{POLICY_NAME}" ON "{table}"')
    op.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
