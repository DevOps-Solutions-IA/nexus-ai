"""Automated tenant-schema safety gate (NXS-TENANT-003, permanent architectural rule).

Every table marked ``info["tenant_scoped"]`` in the ORM metadata must satisfy the tenant
contract. The static check runs with no database; the live check additionally verifies
that the migrated PostgreSQL schema has RLS ENABLED, FORCED and a policy on each such
table. A future engineer who declares a table tenant-scoped but forgets RLS fails CI.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Table, text
from sqlalchemy.ext.asyncio import AsyncConnection

from nexus_ai.infrastructure.orm import (
    TENANT_OWNED,
    TENANT_SCOPED_KEY,
    TENANT_SELF,
    Base,
    TenantOwnedMixin,
)


@dataclass(slots=True)
class SchemaGuardResult:
    ok: bool
    tables_checked: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    def as_payload(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "tables_checked": sorted(self.tables_checked),
            "violations": sorted(self.violations),
        }


def tenant_tables() -> list[tuple[Table, str]]:
    import nexus_ai.domain.registry  # noqa: F401 - populate Base.metadata with every model

    result: dict[str, tuple[Table, str]] = {}
    # Authoritative source: any mapped class inheriting TenantOwnedMixin is tenant-owned,
    # whether or not it also carries an explicit ``info`` marker on ``__table_args__``.
    for mapper in Base.registry.mappers:
        klass = mapper.class_
        if isinstance(klass, type) and issubclass(klass, TenantOwnedMixin):
            table = mapper.local_table
            if isinstance(table, Table):
                result[table.name] = (table, TENANT_OWNED)
    # Explicit markers cover self-scoped tables (organizations) and raw Table() probes.
    for table in Base.metadata.sorted_tables:
        marker = table.info.get(TENANT_SCOPED_KEY)
        if marker in {TENANT_OWNED, TENANT_SELF}:
            result.setdefault(table.name, (table, str(marker)))
    return [result[name] for name in sorted(result)]


def _static_violations(table: Table, marker: str) -> list[str]:
    problems: list[str] = []
    if marker == TENANT_SELF:
        if "id" not in table.columns:
            problems.append(f"{table.name}: self-scoped table has no id column")
        return problems

    column = table.columns.get("organization_id")
    if column is None:
        problems.append(f"{table.name}: tenant-owned table is missing organization_id")
        return problems
    if column.nullable:
        problems.append(f"{table.name}: organization_id must be NOT NULL")
    if "UUID" not in column.type.__class__.__name__.upper():
        problems.append(f"{table.name}: organization_id must be a UUID column")
    if not any(fk.column.table.name == "organizations" for fk in column.foreign_keys):
        problems.append(f"{table.name}: organization_id must reference organizations")
    indexed = any("organization_id" in {c.name for c in index.columns} for index in table.indexes)
    if not (column.index or indexed):
        problems.append(f"{table.name}: organization_id must be indexed")
    return problems


def check_static() -> SchemaGuardResult:
    result = SchemaGuardResult(ok=True)
    for table, marker in tenant_tables():
        result.tables_checked.append(table.name)
        result.violations.extend(_static_violations(table, marker))
    result.ok = not result.violations
    return result


async def check_live(connection: AsyncConnection) -> SchemaGuardResult:
    result = check_static()
    if connection.dialect.name != "postgresql":  # pragma: no cover - only PostgreSQL enforces RLS
        result.violations.append("live schema guard requires PostgreSQL")
        result.ok = False
        return result

    for table, _marker in tenant_tables():
        row = (
            await connection.execute(
                text(
                    "SELECT c.relrowsecurity, c.relforcerowsecurity, "
                    "(SELECT count(*) FROM pg_policies p WHERE p.tablename = c.relname) "
                    "FROM pg_class c WHERE c.relname = :name AND c.relkind = 'r'"
                ),
                {"name": table.name},
            )
        ).one_or_none()
        if row is None:
            result.violations.append(f"{table.name}: not present in the migrated schema")
            continue
        enabled, forced, policies = bool(row[0]), bool(row[1]), int(row[2])
        if not enabled:
            result.violations.append(f"{table.name}: RLS is not ENABLED")
        if not forced:
            result.violations.append(f"{table.name}: RLS is not FORCED")
        if policies < 1:
            result.violations.append(f"{table.name}: has no RLS policy")
    result.ok = not result.violations
    return result
