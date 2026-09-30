"""Actual grant-table ACL inventory is not a fabricated administration audit."""

import asyncpg
import pytest

from tests.conftest import SUPERUSER_DSN

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_platform_grant_mutation_acl_inventory(migrated_database):
    admin = await asyncpg.connect(SUPERUSER_DSN)
    try:
        permissions = await admin.fetchrow(
            "SELECT has_table_privilege('nexus_runtime','platform_grants','SELECT') AS read, "
            "has_table_privilege('nexus_runtime','platform_grants','INSERT') AS insert, "
            "has_table_privilege('nexus_runtime','platform_grants','UPDATE') AS update, "
            "has_table_privilege('nexus_runtime','platform_grants','DELETE') AS delete, "
            "has_table_privilege('nexus_audit_platform','platform_grants','INSERT') "
            "AS audit_insert, "
            "has_table_privilege('nexus_audit_platform','platform_grants','UPDATE') "
            "AS audit_update, "
            "has_table_privilege('nexus_audit_platform','platform_grants','DELETE') AS audit_delete"
        )
        assert dict(permissions) == {
            "read": True,
            "insert": True,
            "update": True,
            "delete": False,
            "audit_insert": False,
            "audit_update": False,
            "audit_delete": False,
        }
    finally:
        await admin.close()
