"""Permanent tenant-schema CI gate (NXS-TENANT-003, section 93).

Fails when any ORM table marked ``tenant_scoped`` is unsafe — statically (missing
organization_id / FK / index) or in the migrated PostgreSQL schema (RLS not enabled,
not forced, or no policy). Run after ``alembic upgrade head``:

    uv run python -m scripts.nxs_schema_guard [--static-only]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from sqlalchemy.ext.asyncio import create_async_engine

from nexus_ai.core.config import Settings
from nexus_ai.infrastructure.schema_guard import check_live, check_static


async def _run(static_only: bool) -> int:
    if static_only:
        result = check_static()
    else:
        engine = create_async_engine(Settings().database.migration_async_dsn())
        try:
            async with engine.connect() as connection:
                result = await check_live(connection)
        finally:
            await engine.dispose()
    print(json.dumps(result.as_payload(), indent=2, sort_keys=True))
    return 0 if result.ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="nxs_schema_guard")
    parser.add_argument("--static-only", action="store_true")
    arguments = parser.parse_args()
    return asyncio.run(_run(arguments.static_only))


if __name__ == "__main__":
    sys.exit(main())
