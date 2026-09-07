"""Idempotent database role bootstrap for local and CI (NXS-SEC-003).

Creates the ``nexus_migration`` and ``nexus_runtime`` roles by running
``infrastructure/postgres/roles.sql`` as a superuser. Safe to re-run. Never used at
application request runtime.

    uv run python -m scripts.nxs_dbadmin bootstrap
      [--superuser-dsn postgresql://nexus_local:local-development-only@127.0.0.1:15432/nexus_local]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

_DEFAULT_SUPERUSER_DSN = (
    "postgresql://nexus_local:local-development-only@127.0.0.1:15432/nexus_local"
)
_ROLES_SQL = Path(__file__).resolve().parents[2] / "infrastructure/postgres/roles.sql"


def _normalize(dsn: str) -> str:
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _bootstrap(dsn: str) -> None:
    import asyncpg

    sql = _ROLES_SQL.read_text(encoding="utf-8")
    connection = await asyncpg.connect(_normalize(dsn), timeout=15)
    try:
        await connection.execute(sql)
        roles = await connection.fetch(
            "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles "
            "WHERE rolname IN ('nexus_migration', 'nexus_runtime') ORDER BY rolname"
        )
    finally:
        await connection.close()
    for role in roles:
        print(
            f"role {role['rolname']}: superuser={role['rolsuper']} bypassrls={role['rolbypassrls']}"
        )
    if len(roles) != 2:
        raise SystemExit("bootstrap did not create both roles")
    if any(role["rolsuper"] or role["rolbypassrls"] for role in roles):
        raise SystemExit("a bootstrapped role can bypass tenancy")


def main() -> int:
    parser = argparse.ArgumentParser(prog="nxs_dbadmin")
    parser.add_argument("command", choices=("bootstrap",))
    parser.add_argument(
        "--superuser-dsn",
        default=os.getenv("NXS_DB_SUPERUSER_DSN", _DEFAULT_SUPERUSER_DSN),
    )
    arguments = parser.parse_args()
    asyncio.run(_bootstrap(arguments.superuser_dsn))
    print("NXS DB BOOTSTRAP: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
